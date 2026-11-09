`include "defines.vh"

// ============================================================================
//  cp —— 命令处理器(Command Processor)
//
//  职责:整颗 GPU 唯一的命令入口 + 顺序调度器。host 只用 MMIO 跟它说话;它从全
//  局内存里的"命令环"一条条取命令,校验、执行或分发,然后推进 HEAD、加信号量、
//  打中断。SM / 2D / GEMM 都不会自己启动,只能被它踢一脚。
//
//  三条硬规则(所有状态都围着这三条转):
//    1) in-order,一次只做一条命令:HEAD 只在"这条命令确认完成"的那一拍前进
//       (head_we,见 ST_UPD_FIN);命令失败则 HEAD 不动 + 停机,于是 host 读
//       CMD_HEAD 就知道卡在哪条命令上 —— 这是唯一的调试锚点。
//    2) 命令头里的 wait_id 在"校验之后、执行/分发之前"查(见 ST_DECODE)。
//    3) 任何等待都有上限:TIMEOUT_CYCLES 到了 ⇒ CPERR_TIMEOUT + 停机。
//
//  命令格式(命令环里连续排布,单位 32bit 字):
//    word0             : {length_words[31:16], cmd_flags[15:8], opcode[7:0]}
//    word1             : {signal_id[31:16], wait_id[15:0]}
//    word2..word_{n-1} : 各命令的操作数,由 cmd_decode 逐 opcode 校验
//    0xFFFF = 无信号量 / 无依赖(cp_pkg::SEM_NONE)
//
//  状态机 18 态,按职能分四组(与 cp.md §5 的对照表一致):
//    取命令 : ST_IDLE → ST_LOADW → ST_LOADW2 → ST_DECODE
//    执行   : ST_EXEC → { ST_MEM | ST_PARAMS | ST_LOADK→ST_LOADK_ND
//                        | ST_LAUNCH→ST_WAIT_SM | ST_ENG_SEND→ST_WAIT_ENG
//                        | ST_POLL→ST_POLL_WAIT }
//    提交   : ST_UPD_FIN(需要加信号量时经 ST_MEM 去 ST_SEM_WR 再回来)
//    错误   : ST_ERR(只认 CP_RESET,不认别的)
//
//  读懂本文件最关键的两个"小协议":
//    · 内存口是 req / ack / rsp 两段式:req 电平保持到 ack;数据真正回来的那拍
//      给 rsp(单拍)。CP 一次只有一个操作在飞,所以 ST_MEM 里一个 req_sent 位
//      就能管住握手,再用 cp_ret_state 记住"做完回哪个态"—— 一个状态复用给
//      读行/读字/写字/信号量读写等全部 7 类访问。
//    · 64B 行缓冲(line_buf / line_base / line_valid)把"连续取十几个字"摊成
//      1 次突发。它的生命周期严格等于一条命令(ST_UPD_FIN 里无条件清 valid),
//      因为 host 可以把 HEAD / QUEUE_BASE 搬回已消费区域,跨命令缓存会读到
//      旧命令的字节(§6.1 的正确性要求,不是优化取舍)。
//
//  寄存器只有一份(cp_regs.sv),两个访问口:host 走 AXI4-Lite(ext_wr/ext_rd),
//  状态机直连(int_wr / int_addr / int_rdata)做零延迟读写;两者撞车时状态机优先。
// ============================================================================

module cp import cp_pkg::*; #(
    parameter logic [31:0] QUEUE_BASE_DEF = `CMD_QUEUE_BASE,
    parameter logic [31:0] QUEUE_SIZE_DEF = 32'd4096,
    parameter int          LINE_WORDS     = 16                 // 64B 行
) (
    input  wire         clk            ,
    input  wire         rst            ,

    // host 侧:AXI4-Lite 从口(4KB MMIO 窗口)
    input  wire         s_awvalid      ,
    output logic        s_awready      ,
    input  wire  [31:0] s_awaddr       ,
    input  wire         s_wvalid       ,
    output logic        s_wready       ,
    input  wire  [31:0] s_wdata        ,
    input  wire  [ 3:0] s_wstrb        ,
    output logic        s_bvalid       ,
    input  wire         s_bready       ,
    output logic [ 1:0] s_bresp        ,
    input  wire         s_arvalid      ,
    output logic        s_arready      ,
    input  wire  [31:0] s_araddr       ,
    output logic        s_rvalid       ,
    input  wire         s_rready       ,
    output logic [31:0] s_rdata        ,
    output logic [ 1:0] s_rresp        ,
    output wire         irq            ,

    // 内存
    output logic        mem_rd_req     ,
    output logic [31:0] mem_rd_addr    ,
    output logic        mem_rd_line    ,
    input  wire         mem_rd_ack     ,
    input  wire         mem_rd_vld     ,   // 本拍有一个 32 位字
    input  wire [31:0]  mem_rd_word    ,
    input  wire         mem_rd_last    ,   // 本次读的最后一个字
    input  wire         mem_rd_err     ,
    output logic        mem_wr_req     ,
    output logic [31:0] mem_wr_addr    ,
    output logic [31:0] mem_wr_data    ,
    output logic [ 3:0] mem_wr_strb    ,
    input  wire         mem_wr_ack     ,
    input  wire         mem_wr_rsp     ,
    input  wire         mem_wr_err     ,

    //命令缓存统计(只读,给仿真观察用)
    output logic [31:0] cc_hit         ,   // CP 的真实请求命中次数
    output logic [31:0] cc_miss        ,   // CP 的真实请求缺失次数
    output logic [31:0] cc_pref        ,   // 预取装进缓存的行数

    //SM
    output logic [ 1:0] sm_launch_valid,
    output logic [31:0] sm_launch_pc   ,
    output logic [ 3:0] sm_launch_warps,
    output logic [ 3:0] sm_launch_flags,
    input  wire  [ 1:0] sm_done        ,
    input  wire  [ 3:0] sm0_error      ,
    input  wire  [ 3:0] sm1_error      ,
    input  wire  [31:0] sm0_fault_pc   ,
    input  wire  [31:0] sm1_fault_pc   ,
    output logic [ 1:0] sm_err_clr     ,

    // 指令 SRAM 预加载
    output logic [ 1:0] sram_load_en   ,
    output logic [12:0] sram_load_addr ,
    output logic [31:0] sram_load_data ,
    output logic [ 1:0] sram_load_last ,
    input  wire  [ 1:0] sram_load_done ,
    input  wire  [ 1:0] sram_err_ovf   ,

    // 引擎侧
    output logic        eng_valid      ,
    output logic [ 1:0] eng_engine     ,
    output logic [ 7:0] eng_opcode     ,
    output logic [15:0] eng_len        ,
    output logic        eng_wvalid     ,
    output logic [31:0] eng_wdata      ,
    output logic        eng_wlast      ,
    input  wire         eng_ready      ,
    input  wire  [ 2:0] gemm_status    ,
    input  wire  [ 7:0] gemm_error     ,
    input  wire  [ 2:0] twod_status    ,
    input  wire  [ 7:0] twod_error     ,
    output logic [ 1:0] eng_err_clr
);
    // 微操作(内存访问):ST_MEM 靠它 + req_sent + cp_ret_state 复用成一个通用执行器
    typedef enum logic [1:0] { OP_NONE=0, OP_RD_LINE=1, OP_RD_WORD=2, OP_WR_WORD=3 } mem_op_t;

    // ── 主状态机:18 态,一态一件事 ──────────────────────────────────────────
    //   取命令:
    //     ST_IDLE      等 DOORBELL;队列空(head==tail)时只回 ack 不前进
    //     ST_LOADW     取第 idx 个字:idx=0 抽 word0(len/flags/opcode),
    //                  idx=1 抽 word1(signal/wait)并做长度 + 队列边界三判
    //     ST_LOADW2    补齐命令体到 min(len,16) 字,然后锁出 w2/w3/w4
    //     ST_DECODE    跑 cmd_decode + 各命令的前置条件;决定进 EXEC 还是先轮询
    //   执行(每一个分支都是"设好 mop/参数 → 交给别处做 → 做完回来"):
    //     ST_EXEC      按 opcode 分派,本身几乎不产生时序(组合地设下个态)
    //     ST_MEM       唯一的内存执行器:读行/读字/写字,完成回 cp_ret_state
    //     ST_PARAMS    逐字写常量区;发起写时 idx 就 ++,直接回自己(省一趟回程)
    //     ST_LOADK     逐字读镜像 → 写指令 SRAM(按 sm_mask 广播)+ 喂 CRC
    //     ST_LOADK_ND  等 SRAM 装完,校验镜像头 + CRC;全过才置 SM_KERNEL_MASK
    //     ST_LAUNCH    一拍分发(sm_launch_valid 由组合逻辑直接给出)
    //     ST_WAIT_SM   等涉及的 SM 全部 DONE;任一 SM 报错 ⇒ SM_FAULT 且不写信号量
    //     ST_ENG_SEND  先推引擎头(eng_valid),再推 payload(eng_ready 背压)
    //     ST_WAIT_ENG  等引擎 DONE && !BUSY
    //     ST_POLL      发一次目标字读(信号量 / 任意同步地址)
    //     ST_POLL_WAIT 判读回的值,并用 poll_gap 做 8 拍节流(重读周期约 12 拍)
    //   提交 / 错误:
    //     ST_UPD_FIN   推进 HEAD、清行缓存、置 IRQ;要加信号量时先去 ST_SEM_WR
    //     ST_SEM_WR    SEM[id] += 1(读-改-写只由 CP 单点完成,不做 atomics)
    //     ST_ERR       锁首个错误后停机,只认 CP_RESET
    typedef enum logic [4:0] {
        ST_IDLE     , ST_LOADW  , ST_LOADW2     , ST_DECODE   , ST_EXEC     ,
        ST_MEM      , ST_PARAMS , ST_LOADK      , ST_LOADK_ND , ST_LAUNCH   ,
        ST_WAIT_SM  , ST_ENG_SEND, ST_WAIT_ENG  , ST_SEM_WR   , ST_POLL     ,
        ST_POLL_WAIT, ST_UPD_FIN, ST_ERR
    } cp_state_t;

    cp_state_t cp_state, cp_ret_state;
    mem_op_t   mop;
    logic      req_sent;

    // ── 命令头(取自 word0/word1)与操作数 ────────────────────────────────────
    logic [15:0] len_q;             // word0[31:16] 命令总字数(含头和体)
    logic [ 7:0] flags_q, op_q;     // word0[15:8] cmd_flags ; word0[7:0] opcode
    logic [15:0] sig_q, wait_q;     // word1[31:16] signal_id ; word1[15:0] wait_id
    logic [31:0] cmd_w [0:15];      // 本条命令的前 16 字;更长的部分(PARAMS)按需从行缓存补
    logic [11:0] idx;              // 通用循环下标(PARAMS ≤ 260 / LOAD_KERNEL ≤ 4096)
    logic [31:0] scratch, scratch2, mem_wr_data_q;
                                   // scratch  = 数据暂存(轮询读回值 / CRC 输入 / 行字)
                                   // scratch2 = 本次内存访问的地址(ST_MEM 直接送出)
                                   // mem_wr_data_q = 写数据
    logic [31:0] w2_q, w3_q;        // word2 / word3 原值(各命令按需解释)
    logic [ 3:0] w4_q;              // LOAD_KERNEL 的 sm_mask / LAUNCH 的 flags
    logic        sig_ok_q;          // 本条命令带信号量(sig_q != SEM_NONE)
    logic        sig_rd_sent;         // UPD_FIN:信号量读已发起, 等写回后再进收尾拍

    // ── 命令级暂存(每条命令开始时由 ST_EXEC 填) ────────────────────────────
    logic [15:0] params_n;          // PARAMS 的字数 N
    logic [31:0] params_dst;        // PARAMS 目标地址 = CONST_BASE + dst_off
    logic [31:0] load_src;          // LOAD_KERNEL 的镜像源地址
    logic [11:0] load_words;        // 镜像字数(≤ 4096)
    logic [31:0] img_hdr [0:7];     // 镜像头 = 镜像前 32B(8 字),流式装的时候顺手抓
    logic [ 1:0] load_sm_mask;      // LOAD_KERNEL 装给哪些 SM
    logic [ 3:0] launch_warps_q;    // 每个 SM 拿到的 warp 数(≤ 4)
    logic [ 3:0] launch_flags_q;
    logic [31:0] launch_pc_q;       // = LAUNCH 的 kernel_pc(指令 SRAM 内的字节 PC)
    logic [ 1:0] sm_sel_q, eng_sel_q;
    logic [31:0] poll_addr, poll_val;   // 轮询目标地址 / 达标值
    logic        poll_dst_upd;          // 轮询成功后去哪:0=ST_EXEC 1=ST_UPD_FIN
    logic [ 7:0]  poll_gap;             // 轮询节流倒计时(8 拍→重读一次)
    logic [31:0] timeout_cnt;           // 当前命令/等待已耗拍数,各处等待态自增
    logic        kernel_active_q;
    logic        eng_hdr_sent;          // 引擎头已推,正在推 payload

    // ── 行缓冲:命令缓存回一行就灌满它,按字消费 ─────────────────────────────
    logic [31:0] line_buf [0:LINE_WORDS-1];
    logic [31:6] line_base;         // 哪一行(地址去掉低 6 位)
    logic        line_valid;

    // ── 命令缓存(cmd_cache)接口:整行读全走它,命中 2 拍 ────────────────────
    logic                     cc_req;       // 请求一整行(电平,直到 cc_rsp)
    logic [31:6]              cc_req_line;
    logic                     cc_rsp;
    logic [31:6]              cc_rsp_line;
    logic [LINE_WORDS*32-1:0] cc_rsp_data;
    logic                     cc_rsp_err;
    logic [31:6]              cc_ring_base; // 命令环首行
    logic [31:6]              cc_ring_end;  // 命令环最后一行的行号 + 1
    logic                     cc_pub;       // 单拍:有区域被(重新)发布
    logic [31:6]              cc_pub_s;     // 发布区域 [s, e) 的行号
    logic [31:6]              cc_pub_e;
    logic                     cc_inval_all; // 整片失效
    logic                     cc_busy;      // 缓存占着内存口
    logic                     cc_m_rd_req;
    logic [31:0]              cc_m_rd_addr;
    logic [31:0]              cc_ctr_hit, cc_ctr_miss, cc_ctr_pref;
    logic [31:0]              cc_ctr_pub, cc_ctr_inval, cc_ctr_rdline;
    logic                     cc_rd_ack, cc_rd_vld, cc_rd_last, cc_rd_err;
    logic [31:0]              cc_rd_word;
    logic                     cp_rd_ack, cp_rd_vld, cp_rd_last, cp_rd_err, cp_rd_req;
    logic                     cp_port_busy;

    // 寄存器文件接口
    logic [31:0] head, tail, queue_base, queue_size, sem_base, timeout_cycles;
    logic [ 1:0] kernel_mask;
    /* verilator lint_off UNUSEDSIGNAL */   // LAUNCH 只用 size/flags 做交叉核对(entry 由驱动给)
    logic [31:0] img_entry [0:1];
    /* verilator lint_on UNUSEDSIGNAL */
    logic [31:0] img_size [0:1], img_flags [0:1];
    logic [ 3:0] cp_error;
    logic        doorbell_pend;
    logic        ext_wr, ext_rd, cp_reset_evt, doorbell_evt, chk_wr_ok;
    logic [11:0] ext_addr, chk_addr, int_addr;
    logic [31:0] ext_wdata, int_rdata, regs_ext_rdata;
    logic        int_wr, head_we, img_we;
    logic [11:0] int_wr_addr;
    logic [31:0] int_wr_wdata, head_wdata, img_entry_w, img_size_w, img_flags_w;
    logic [ 1:0] img_sm_mask;
    logic [ 3:0] cp_err_we_data;
    logic        cp_err_we, doorbell_ack;
    logic [ 7:0]  irq_set;
    logic [ 7:0]  ev_irq_cmd;
    logic        ev_cp_error, ev_cmd_done, ev_launch_done, ev_2d_done, ev_gemm_done;

    // ── 状态/指针组合量:CP_STATUS 的两个位、HEAD 的推进量、各路"达标/超时"判据 ──
    wire queued  = (head != tail);              // 队列里还有待处理命令
    wire working = (cp_state != ST_IDLE);       // FSM 正在处理一条命令
    wire busy_i  = queued | working;          
    wire idle_i  = ~queued & ~working & (cp_error == CPERR_NONE);  

    // HEAD 前进 len*4。注意"未回绕值"next_head_raw 会同时用于三处判定:
    //   · 上界:next_head_raw 越过 base+size ⇒ CMD_ERROR(命令不许跨环末尾)
    //   · 上界:next_head_raw 越过 tail      ⇒ CMD_ERROR(不许读 host 还没写的区域)
    //   · 收尾:next_head_raw == tail        ⇒ 这就是最后一条,做完回 IDLE 并 ack
    // 只有"恰好等于 base+size"才允许回绕(驱动用 NOP 填平末尾),所以 next_head
    // 的减法分支只在那一拍生效。
    wire [31:0] next_head_raw = head + {16'h0, len_q} * 32'd4;
    wire [31:0] next_head     = (next_head_raw >= queue_base + queue_size)
                                ? (next_head_raw - queue_size) : next_head_raw;
    wire        is_last_cmd   = (next_head_raw == tail);

    // 只挑本条命令涉及的 SM / 引擎来看,别的组的错误与本次命令无关
    wire [3:0] sm_err_sel = (sm_sel_q[0] ? sm0_error : 4'h0) |
                            (sm_sel_q[1] ? sm1_error : 4'h0);
    wire [2:0] eng_status_sel = (eng_sel_q == ENG_2D) ? twod_status : gemm_status;
    wire       eng_status_err = eng_status_sel[ENG_ST_ERROR];
    wire       eng_status_ok  = eng_status_sel[ENG_ST_DONE] && !eng_status_sel[ENG_ST_BUSY];
    wire       timeout_hit    = (timeout_cnt >= timeout_cycles);   // 所有等待态共用

    // ── 取指辅助:行命中就吃缓冲里的字,没命中就退化成一次 64B 行读 ───────────
    /* verilator lint_off UNUSEDSIGNAL */   // a 的高位在 line_hit 里比过, 这里只看行内偏移
    function automatic logic line_hit(input logic [31:0] a);
        return line_valid && (a[31:6] == line_base);
    endfunction
    function automatic logic [31:0] line_word(input logic [31:0] a);
        return line_buf[a[5:2]];
    endfunction
    function automatic logic [31:0] cmd_addr(input logic [11:0] i);
        return head + {20'h0, i} * 32'd4;      // 命令第 i 个字的全局地址
    endfunction
    /* verilator lint_on UNUSEDSIGNAL */

    // LAUNCH 的前置条件
    function automatic logic [3:0] launch_check(
        input logic [31:0] kpc, input logic [3:0] nw, input logic [3:0] lflags,
        input logic [1:0] ssel
    );
        logic [1:0] inv;
        inv = ssel;                                                      // 本条命令要用的 SM 组
        if (inv[0] && (sm0_error != 4'h0)) return CPERR_STALE_SM_ERROR;  // 上次错误没清
        if (inv[1] && (sm1_error != 4'h0)) return CPERR_STALE_SM_ERROR;
        if (kpc[1:0] != 2'b00)             return CPERR_CMD;             // pc 4B 对齐
        if (inv[0] && (kpc >= img_size[0])) return CPERR_CMD;            // pc 在镜像内
        if (inv[1] && (kpc >= img_size[1])) return CPERR_CMD;
        if ((kernel_mask & inv) != inv)     return CPERR_CMD;            // 涉及的 SM 都装过镜像
        if (inv[0] && (img_flags[0][3:0] != lflags)) return CPERR_CMD;   // flags 与镜像头逐位一致
        if (inv[1] && (img_flags[1][3:0] != lflags)) return CPERR_CMD;
        if ((lflags[FLAG_USES_BARRIER] | lflags[FLAG_USES_SHARED] |
             lflags[FLAG_REQUIRES_SINGLE_SM]) && (nw > 4'd4))
            return CPERR_LAUNCH_REJECT;                                  // 单 SM 语义却跨了 SM
        return CPERR_NONE;
    endfunction

    // WRITE_REG 的额外校验:改队列配置有"值"与"时机"两道门
    // 注意两条写路径的报错方式不同:WRITE_REG 命令走这里 ⇒ 报 CP_ERROR;
    // host 直写 MMIO 则由 cp_regs 直接丢弃该写(idle_i 门控),不置错误。
    wire wr_que_base = (w2_q == {24'h0, `MMIO_CMD_QUEUE_BASE});
    wire wr_que_size = (w2_q == {24'h0, `MMIO_CMD_QUEUE_SIZE});
    wire que_base_val_ok = (w3_q >= `MMIO_END) && (w3_q[1:0] == 2'b00);   // 4B 对齐且不在 MMIO 窗口
    wire que_size_val_ok = (w3_q >= 32'd8) && (w3_q[2:0] == 3'b000);      // ≥8 且 8 的倍数
    wire que_cfg_state_ok = is_last_cmd & ~kernel_active_q;              // 时机:本命令是最后一条且无 kernel 在跑
    wire wr_reg_bad_state = (wr_que_base | wr_que_size) & ~que_cfg_state_ok;
    wire wr_reg_bad_value = (wr_que_base & ~que_base_val_ok) |
                            (wr_que_size & ~que_size_val_ok);

    wire [1:0] sm_sel_c = (cmd_w[3][3:0] > 4'd4) ? 2'b11 : 2'b01;
    wire [3:0] launch_err_c = launch_check(cmd_w[2], cmd_w[3][3:0], cmd_w[4][3:0], sm_sel_c);

    axil_slave u_axil (
        .clk       (clk           ), 
        .rst       (rst           ),
        .s_awvalid (s_awvalid     ), 
        .s_awready (s_awready     ), 
        .s_awaddr  (s_awaddr      ),
        .s_wvalid  (s_wvalid      ),   
        .s_wready  (s_wready      ),   
        .s_wdata   (s_wdata       ), 
        .s_wstrb   (s_wstrb       ),
        .s_bvalid  (s_bvalid      ),   
        .s_bready  (s_bready      ),   
        .s_bresp   (s_bresp       ),
        .s_arvalid (s_arvalid     ), 
        .s_arready (s_arready     ), 
        .s_araddr  (s_araddr      ),
        .s_rvalid  (s_rvalid      ),   
        .s_rready  (s_rready      ),   
        .s_rdata   (s_rdata       ), 
        .s_rresp   (s_rresp       ),
        .reg_wr    (ext_wr        ), 
        .reg_addr  (ext_addr      ), 
        .reg_wdata (ext_wdata     ),
        .reg_rd    (ext_rd        ), 
        .reg_rdata (regs_ext_rdata)
    );

    logic [7:0] irq_status_w, irq_enable_w;
    cp_regs #(
        .QUEUE_BASE_DEF(QUEUE_BASE_DEF), .QUEUE_SIZE_DEF(QUEUE_SIZE_DEF)
    ) u_regs (
        .clk         (clk           ), 
        .rst         (rst           ),
        .ext_wr      (ext_wr        ), 
        .ext_addr    (ext_addr      ), 
        .ext_wdata   (ext_wdata     ),
        .ext_rd      (ext_rd        ), 
        .ext_rdata   (regs_ext_rdata),

        .chk_addr    (chk_addr      ), 
        .chk_wr_ok   (chk_wr_ok     ),
        .cp_reset_evt(cp_reset_evt  ), 
        .doorbell_evt(doorbell_evt  ),

        .busy_i      (busy_i        ), 
        .idle_i      (idle_i        ),

        .sm_done_i     (sm_done     ), 
        .sm0_error_i   (sm0_error   ), 
        .sm1_error_i   (sm1_error   ),
        .sm0_fault_pc_i(sm0_fault_pc), 
        .sm1_fault_pc_i(sm1_fault_pc),
        .gemm_status_i (gemm_status ), 
        .gemm_error_i  (gemm_error  ),
        .twod_status_i (twod_status ), 
        .twod_error_i  (twod_error  ),
        .sm_err_clr    (sm_err_clr  ), 
        .eng_err_clr   (eng_err_clr ),

        .head_o         (head          ), 
        .tail_o         (tail          ), 
        .queue_base_o   (queue_base    ), 
        .queue_size_o   (queue_size    ),
        .sem_base_o     (sem_base      ), 
        .timeout_o      (timeout_cycles),
        .kernel_mask_o  (kernel_mask   ), 
        .img_entry_o    (img_entry     ), 
        .img_size_o     (img_size      ),
        .img_flags_o    (img_flags     ), 
        .cp_error_o     (cp_error      ),
        .irq_status_o   (irq_status_w  ), 
        .irq_enable_o   (irq_enable_w  ),
        .doorbell_pend_o(doorbell_pend ),
        .int_addr       (int_addr      ), 
        .int_rdata      (int_rdata     ),
        .int_wr         (int_wr        ), 
        .int_wr_addr    (int_wr_addr   ), 
        .int_wr_wdata   (int_wr_wdata  ),
        
        .head_we        (head_we       ), 
        .head_wdata     (head_wdata    ),
        .tail_we        (1'b0          ), 
        .tail_wdata     (32'h0         ),
        .cp_err_we      (cp_err_we     ), 
        .cp_err_wdata   (cp_err_we_data),
        .irq_set        (irq_set       ), 
        .doorbell_ack   (doorbell_ack  ),
        .img_we         (img_we        ), 
        .img_sm_mask    (img_sm_mask   ),
        .img_entry_w    (img_entry_w   ), 
        .img_size_w     (img_size_w    ), 
        .img_flags_w    (img_flags_w   ),
        .irq_o          (irq           )
    );

    // IRQ_STATUS/IRQ_ENABLE 只在 MMIO 读口用得上;这里留两个观测点(波形/调试)
    `UNUSED_VAR(irq_status_w)
    `UNUSED_VAR(irq_enable_w)

    logic       dec_ok, dec_is_engine, dec_is_vendor;
    logic [3:0] dec_err;
    logic [1:0] dec_engine;
    cmd_decode u_dec (
        .len      (len_q         ), 
        .flags    (flags_q[7:2]  ), 
        .opcode   (op_q          ), 
        .signal_id(sig_q         ), 
        .wait_id  (wait_q        ),
        .cmd_w    (cmd_w         ),
        .ok       (dec_ok        ), 
        .err      (dec_err       ), 
        .is_engine(dec_is_engine ), 
        .engine   (dec_engine    ),
        .is_vendor(dec_is_vendor )
    );
    `UNUSED_VAR(dec_is_engine)          // 引擎分派在 ST_EXEC 里按 opcode 判, 这两个留作端口语义
    `UNUSED_VAR(dec_is_vendor)
    
    logic        crc_init, crc_en;
    logic [31:0] crc_val;
    crc32_word u_crc (
        .clk  (clk      ), 
        .rst  (rst      ), 
        .init (crc_init ), 
        .en   (crc_en   ), 
        .data (scratch  ), 
        .crc  (crc_val  )
    );

    // ── 内存口分时复用 ──────────────────────────────────────────────────────
    // 整行读由 cmd_cache 发(它自己会预取下一行);CP 自己只做单字读和单字写。
    // 两者互斥:缓存只在 CP 没有单字操作在飞时才开始回填,CP 只在缓存没占口时才发请求。
    assign cp_port_busy = (mop == OP_RD_WORD) || (mop == OP_WR_WORD);
    assign cp_rd_req    = (mop == OP_RD_WORD) & ~req_sent & ~cc_busy;

    assign mem_rd_req  = cc_busy ? cc_m_rd_req : cp_rd_req;
    assign mem_rd_addr = cc_busy ? cc_m_rd_addr : scratch2;
    assign mem_rd_line = cc_busy;          // 缓存只读整行;CP 自己只读单字
    assign mem_wr_req  = (mop == OP_WR_WORD) & ~req_sent & ~cc_busy;
    assign mem_wr_addr = scratch2;
    assign mem_wr_data = mem_wr_data_q;
    assign mem_wr_strb = 4'hF;

    // 读回来的数据是 AXI R 的节奏(一拍一个 32 位字),所以这里按"谁占着口"分流
    assign cc_rd_ack  = cc_busy & mem_rd_ack;
    assign cc_rd_vld  = cc_busy & mem_rd_vld;
    assign cc_rd_last = cc_busy & mem_rd_last;
    assign cc_rd_word = mem_rd_word;
    assign cc_rd_err  = cc_busy & mem_rd_err;
    assign cp_rd_ack  = ~cc_busy & mem_rd_ack;
    assign cp_rd_vld  = ~cc_busy & mem_rd_vld;
    assign cp_rd_last = ~cc_busy & mem_rd_last;
    assign cp_rd_err  = ~cc_busy & mem_rd_err;
    /* verilator lint_off UNUSEDSIGNAL */
    wire cp_rd_last_un = cp_rd_last;   // 单字读固定一拍, last 与 vld 同拍, 用不着
    /* verilator lint_on UNUSEDSIGNAL */

    // 单字读/写共用一个 ST_MEM,这里按 mop 把两组响应选成一份
    wire        mem_ack = (mop == OP_RD_WORD) ? cp_rd_ack : mem_wr_ack;
    wire        mem_vld = (mop == OP_RD_WORD) ? cp_rd_vld : 1'b0;
    wire [31:0] mem_word = mem_rd_word;
    wire        mem_err = (mop == OP_RD_WORD) ? cp_rd_err : mem_wr_err;

    // ── 失效按"范围"而不是"全片" ──────────────────────────────────────────
    // 驱动协议:先把命令字节写进环,再写 TAIL(或 DOORBELL)把它发布出来。因此
    //   * 写 TAIL 前进的那一段 [tail_pub, 新 tail) 才是"可能变了"的区域 —— 只失效它;
    //   * 没写 TAIL 就敲门(就地重发布)则失效整个活跃区 [head, tail);
    //   * 只有 CP_RESET 与搬动整个读区域的 CMD_HEAD / QUEUE_BASE / QUEUE_SIZE 才全片失效。
    // 发布事件同时踢一脚预取:缓存会在后台把新区域填好(CP 这时可能在等 SM)。
    wire tail_wr = ext_wr & (ext_addr == {4'h0, `MMIO_CMD_TAIL});
    wire head_wr = ext_wr & (ext_addr == {4'h0, `MMIO_CMD_HEAD});
    wire base_wr = ext_wr & (ext_addr == {4'h0, `MMIO_CMD_QUEUE_BASE});
    wire size_wr = ext_wr & (ext_addr == {4'h0, `MMIO_CMD_QUEUE_SIZE});

    logic [31:6] tail_pub;      // 已经发布过的 tail(行号)
    logic        tail_seen;     // 上一次发布是靠写 TAIL 做的

    assign cc_ring_base = queue_base[31:6];
    assign cc_ring_end  = 26'((queue_base + queue_size) >> 6);

    wire [31:0] tail_pub_b = {tail_pub, 6'b0};
    // 正常情况下 TAIL 只会向前走且不越过环尾;反向写/越界写无法界定范围,退化为全片失效
    wire tail_adv = tail_wr & (ext_wdata > tail_pub_b) & (ext_wdata <= (queue_base + queue_size));
    wire tail_bad = tail_wr & ~tail_adv & (ext_wdata != tail_pub_b);
    // 没写 TAIL 就敲门:host 就地重发布,活跃区 [head, tail) 都可能变
    wire door_repub = doorbell_evt & ~tail_seen & (tail[31:6] != head[31:6]);

    assign cc_inval_all = cp_reset_evt | head_wr | base_wr | size_wr | tail_bad;

    always_comb begin
        cc_pub   = 1'b0;
        cc_pub_s = tail_pub;
        cc_pub_e = tail_pub;
        if (tail_adv) begin
            cc_pub   = 1'b1;
            cc_pub_s = tail_pub;
            cc_pub_e = ext_wdata[31:6];
        end
        else if (door_repub) begin
            cc_pub   = 1'b1;
            cc_pub_s = head[31:6];
            cc_pub_e = tail[31:6];
        end
        else if (cc_inval_all) begin
            // 全片失效之后顺手把活跃区预热回来
            cc_pub_s = head[31:6];
            cc_pub_e = tail[31:6];
            cc_pub   = (head[31:6] != tail[31:6]);
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            tail_pub  <= QUEUE_BASE_DEF[31:6];
            tail_seen <= 1'b0;
        end
        else if (cp_reset_evt) begin
            // CP_RESET:head := tail,环内容不变但整个读区域要重读;
            // 发布指针退回环首,这样下一次写 TAIL 看起来总是"从环首发布"
            tail_pub  <= QUEUE_BASE_DEF[31:6];
            tail_seen <= 1'b0;
        end
        else if (tail_adv) begin
            tail_pub  <= ext_wdata[31:6];
            tail_seen <= 1'b1;
        end
        else if (doorbell_evt) begin
            tail_seen <= 1'b0;
            if (door_repub) tail_pub <= tail[31:6];
        end
        else if (cc_inval_all) begin
            // 整片失效意味着整个读区域要重读(典型场景:驱动把 CMD_HEAD 搬回环首重放)。
            // 发布指针也退回环首:否则"改写同一段区域、TAIL 又写回原值"会被判成
            // 没有前进 ⇒ 一段本该作废的行被当成仍有效(踩过:重放批次读到上一批的字节)。
            tail_pub  <= queue_base[31:6];
            tail_seen <= 1'b0;
        end
    end

    cmd_cache #(
        .ADDR_W     (32        ),
        .CACHE_SIZE (64        ),          // 64 组 × 64B = 整个 4KB 命令环
        .BLOCK_SIZE (LINE_WORDS)
    ) u_cmd_cache (
        .clk        (clk        ),
        .rst        (rst        ),
        .req_valid  (cc_req     ),
        .req_line   (cc_req_line),
        .rsp_valid  (cc_rsp     ),
        .rsp_line   (cc_rsp_line),
        .rsp_data   (cc_rsp_data),
        .rsp_err    (cc_rsp_err ),
        .pub_valid  (cc_pub     ),
        .pub_start_line(cc_pub_s),
        .pub_end_line  (cc_pub_e),
        .inval_all  (cc_inval_all),
        .ring_base_line(cc_ring_base),
        .ring_end_line(cc_ring_end),
        .port_free  (~cp_port_busy),
        .m_rd_req   (cc_m_rd_req),
        .m_rd_addr  (cc_m_rd_addr),
        .m_rd_ack   (cc_rd_ack  ),
        .m_rd_vld   (cc_rd_vld  ),
        .m_rd_word  (cc_rd_word ),
        .m_rd_last  (cc_rd_last ),
        .m_rd_err   (cc_rd_err  ),
        .busy       (cc_busy    ),
        .ctr_hit    (cc_ctr_hit   ),
        .ctr_miss   (cc_ctr_miss  ),
        .ctr_pref   (cc_ctr_pref  ),
        .ctr_pub    (cc_ctr_pub   ),
        .ctr_inval  (cc_ctr_inval ),
        .ctr_rdline (cc_ctr_rdline)
    );

    // 统计口:命中/缺失/预取装载 给仿真观察(发布与失效次数内部留存,不引出)
    assign cc_hit  = cc_ctr_hit;
    assign cc_miss = cc_ctr_miss;
    assign cc_pref = cc_ctr_pref;
    /* verilator lint_off UNUSEDSIGNAL */
    wire [95:0] cc_stat_un = {cc_ctr_rdline, cc_ctr_pub, cc_ctr_inval};
    /* verilator lint_on UNUSEDSIGNAL */

    // 指令 SRAM 预加载(广播到 sm_mask 选中的 SM)
    logic sram_en_q, sram_last_q;
    logic [11:0] sram_addr_q;
    logic [31:0] sram_data_q;
    assign sram_load_en   = {2{sram_en_q}}   & load_sm_mask;
    assign sram_load_addr = {1'b0, sram_addr_q};   // instr_mem 的 13 位口:最高位判越界
    assign sram_load_data = sram_data_q;
    assign sram_load_last = {2{sram_last_q}} & load_sm_mask;

    // 引擎 payload 流
    assign eng_valid  = (cp_state == ST_ENG_SEND) & ~eng_hdr_sent;
    assign eng_engine = eng_sel_q;
    assign eng_opcode = op_q;
    assign eng_len    = len_q;
    wire [4:0] payload_len = len_q[4:0] - 5'd2;      // payload = word2..word_{len-1}
    assign eng_wvalid = (cp_state == ST_ENG_SEND) & eng_hdr_sent & (idx[4:0] < payload_len);
    assign eng_wdata  = cmd_w[idx[3:0] + 4'd2];      // 最多 14 个字(GEMM 是 16 字命令)
    assign eng_wlast  = eng_wvalid & (idx[4:0] == (payload_len - 5'd1));

    // SM 分发:只亮一拍的脉冲 + 广播给两个 SM
    assign sm_launch_valid = (cp_state == ST_LAUNCH) ? sm_sel_q : 2'b00;
    assign sm_launch_pc    = launch_pc_q;
    assign sm_launch_warps = launch_warps_q;
    assign sm_launch_flags = launch_flags_q;

    // HEAD 的推进做成组合脉冲(而不是放在 ST_UPD_FIN 的分支里),因为它必须和
    // "清行缓存"落在同一拍上。条件里的 sig_rd_sent 负责区分信号量 RMW 的两趟:
    //   sig_ok_q=0         → 没有信号量,第一趟就能推进
    //   sig_ok_q=1,sent=0  → 刚发完信号量读,这拍不能推进(等 +1 写回)
    //   sig_ok_q=1,sent=1  → 回到收尾拍,推进
    assign head_we    = (cp_state == ST_UPD_FIN) & (sig_rd_sent | ~sig_ok_q);
    assign head_wdata = next_head;

    // 镜像头的三个字段在 LOADK_ND 通过校验后写回寄存器文件(供之后 LAUNCH 核对)
    assign img_entry_w = img_hdr[1];
    assign img_size_w  = img_hdr[2];
    assign img_flags_w = img_hdr[3];
    assign chk_addr    = cmd_w[2][11:0];
    wire [31:0] load_word_addr = load_src + {20'h0, idx} * 32'd4;   // LOAD_KERNEL 当前字地址
    assign int_addr    = w2_q[11:0];
    assign int_wr_addr = w2_q[11:0];
    assign int_wr_wdata= w3_q;
    logic [7:0] ev_irq;
    always_comb begin                       // CMDS §7.4 的中断位
        ev_irq = 8'h0;
        ev_irq[IRQ_BIT_CP_ERROR]    = ev_cp_error;
        ev_irq[IRQ_BIT_CMD_DONE]    = ev_cmd_done;
        ev_irq[IRQ_BIT_LAUNCH_DONE] = ev_launch_done;
        ev_irq[IRQ_BIT_2D_DONE]     = ev_2d_done;
        ev_irq[IRQ_BIT_GEMM_DONE]   = ev_gemm_done;
    end
    assign irq_set     = ev_irq | ev_irq_cmd;

    always_ff @(posedge clk) begin
        if (rst) begin
            cp_state <= ST_IDLE; cp_ret_state <= ST_IDLE; mop <= OP_NONE; req_sent <= 1'b0;
            len_q <= 16'h0; flags_q <= 8'h0; op_q <= 8'h0;
            sig_q <= SEM_NONE; wait_q <= SEM_NONE;
            idx <= 12'h0; scratch <= 32'h0; scratch2 <= 32'h0; mem_wr_data_q <= 32'h0;
            w2_q <= 32'h0; w3_q <= 32'h0; w4_q <= 4'h0;
            sig_ok_q <= 1'b0; sig_rd_sent <= 1'b0;
            params_n <= 16'h0; params_dst <= 32'h0;
            load_src <= 32'h0; load_words <= 12'h0; load_sm_mask <= 2'b00;
            launch_warps_q <= 4'h0; launch_flags_q <= 4'h0; launch_pc_q <= 32'h0;
            sm_sel_q <= 2'b00; eng_sel_q <= ENG_NONE;
            poll_addr <= 32'h0; poll_val <= 32'h0; poll_dst_upd <= 1'b0; poll_gap <= 8'h0;
            timeout_cnt <= 32'h0;
            kernel_active_q <= 1'b0; eng_hdr_sent <= 1'b0;
            line_valid <= 1'b0; line_base <= 26'h0;
            int_wr <= 1'b0;
            img_we <= 1'b0; img_sm_mask <= 2'b00;
            cp_err_we <= 1'b0; cp_err_we_data <= 4'h0; doorbell_ack <= 1'b0;
            ev_irq_cmd <= 8'h0; ev_cp_error <= 1'b0; ev_cmd_done <= 1'b0;
            ev_launch_done <= 1'b0; ev_2d_done <= 1'b0; ev_gemm_done <= 1'b0;
            crc_init <= 1'b0; crc_en <= 1'b0;
            sram_en_q <= 1'b0; sram_last_q <= 1'b0;
            sram_addr_q <= 12'h0; sram_data_q <= 32'h0;
        end
        else begin
            int_wr         <= 1'b0;
            img_we         <= 1'b0;
            cp_err_we      <= 1'b0;
            doorbell_ack   <= 1'b0;
            crc_init       <= 1'b0;
            crc_en         <= 1'b0;
            sram_en_q      <= 1'b0;
            sram_last_q    <= 1'b0;
            ev_irq_cmd     <= 8'h0;
            ev_cp_error    <= 1'b0;
            ev_cmd_done    <= 1'b0;
            ev_launch_done <= 1'b0;
            ev_2d_done     <= 1'b0;
            ev_gemm_done   <= 1'b0;

            if (cp_reset_evt) begin
                cp_state        <= ST_IDLE; 
                mop             <= OP_NONE; 
                req_sent        <= 1'b0;
                line_valid      <= 1'b0; 
                cc_req          <= 1'b0;
                kernel_active_q <= 1'b0; 
                timeout_cnt     <= 32'h0;
                sig_rd_sent     <= 1'b0;             // 信号量 RMW 半途被复位也要清掉
            end
            else if (cp_state == ST_ERR) begin
                cp_state <= ST_ERR;                       // 停机:等 host 写 CP_RESET
            end
            else case (cp_state)

            // ── 等 DOORBELL。只有一个例外:队列空还来敲门,顺手把 pending 消费掉 ──
            ST_IDLE: begin
                if (doorbell_pend) begin
                    if (head == tail) begin
                        doorbell_ack <= 1'b1;       // 空队列敲门:直接消费掉
                    end
                    else begin
                        idx         <= 12'h0; 
                        timeout_cnt <= 32'h0; 
                        cp_state    <= ST_LOADW;
                    end
                end
            end

            // ── 取第 idx 个字。行没命中就先读整行,读完回本态重来(不是跳过) ──────
            // idx=0/1 时顺手抽命令头;idx=1 这一拍同时充当"长度/队列边界"的检查点。
            ST_LOADW: begin
                if (!line_hit(cmd_addr(idx))) begin
                    scratch2       <= {cmd_addr(idx)[31:6], 6'b0};
                    mop            <= OP_RD_LINE;
                    req_sent       <= 1'b0;
                    cp_ret_state   <= ST_LOADW;
                    cp_state       <= ST_MEM;
                end
                else begin
                    cmd_w[idx[3:0]] <= line_word(cmd_addr(idx));
                    if (idx == 12'h0) begin
                        len_q   <= line_word(cmd_addr(idx))[31:16];
                        flags_q <= line_word(cmd_addr(idx))[15:8];
                        op_q    <= line_word(cmd_addr(idx))[7:0];
                        idx     <= 12'h1;
                    end
                    else begin      // idx == 1:抽 sig/wait, 顺带做原 ST_LENCHK 的长度/队列边界校验
                        sig_q  <= line_word(cmd_addr(idx))[31:16];
                        wait_q <= line_word(cmd_addr(idx))[15:0];
                        if (len_q < 16'd2) begin                 // 任何命令至少 2 字
                            cp_err_we      <= 1'b1;
                            cp_err_we_data <= CPERR_CMD;
                            ev_cp_error    <= 1'b1; 
                            cp_state       <= ST_ERR;
                        end
                        else if (next_head_raw > queue_base + queue_size) begin  // 不许跨环末尾
                            cp_err_we      <= 1'b1;
                            cp_err_we_data <= CPERR_CMD;
                            ev_cp_error    <= 1'b1; 
                            cp_state       <= ST_ERR;
                        end
                        else if ((tail >= head) && (next_head_raw > tail)) begin  // 不许越过 TAIL
                            cp_err_we      <= 1'b1;
                            cp_err_we_data <= CPERR_CMD;
                            ev_cp_error    <= 1'b1; 
                            cp_state       <= ST_ERR;
                        end
                        else begin
                            idx       <= 12'h2;
                            cp_state  <= ST_LOADW2;
                        end
                    end
                end
            end

            // 读命令体(最多 16 字;PARAMS 更长的部分在后面流式读)
            ST_LOADW2: begin
                if ((idx >= 12'd16) || ({4'h0, idx} >= len_q)) begin
                    w2_q <= cmd_w[4'd2]; w3_q <= cmd_w[4'd3]; w4_q <= cmd_w[4'd4][3:0];
                    cp_state   <= ST_DECODE;
                end
                else if (!line_hit(cmd_addr(idx))) begin
                    scratch2 <= {cmd_addr(idx)[31:6], 6'b0};
                    mop      <= OP_RD_LINE;
                    req_sent <= 1'b0;
                    cp_ret_state   <= ST_LOADW2;
                    cp_state       <= ST_MEM;
                end
                else begin
                    cmd_w[idx[3:0]] <= line_word(cmd_addr(idx));
                    idx        <= idx + 12'h1;
                end
            end

            ST_DECODE: begin
                sig_ok_q <= (sig_q != SEM_NONE);     // sig_q 在 LOADW 里已锁存, 这里顺手记下
                if (!dec_ok) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= dec_err;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else if ((op_q == `CMD_WRITE_REG) && wr_reg_bad_value) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_CMD;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else if ((op_q == `CMD_WRITE_REG) && wr_reg_bad_state) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_ILLEGAL_STATE;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else if ((op_q == `CMD_WRITE_REG) && !chk_wr_ok) begin
                    cp_err_we       <= 1'b1;
                     cp_err_we_data <= CPERR_CMD;   // 只读寄存器的写只允许被忽略
                    ev_cp_error     <= 1'b1; 
                    cp_state        <= ST_ERR;
                end
                else if ((op_q == `CMD_LOAD_KERNEL) && kernel_active_q) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_ILLEGAL_STATE;
                    ev_cp_error    <= 1'b1; 
                    cp_state      <= ST_ERR;
                end
                else if ((op_q == `CMD_LAUNCH) && (launch_err_c != CPERR_NONE)) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= launch_err_c;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else if (wait_q != SEM_NONE) begin
                    poll_addr    <= sem_base + {14'h0, wait_q, 2'b00};
                    poll_val     <= 32'd1;              // wait_id 满足 = SEM[id] != 0
                    poll_dst_upd <= 1'b0;               // 成功之后去 ST_EXEC
                    poll_gap     <= 8'h0;
                    timeout_cnt  <= 32'h0;
                    cp_state     <= ST_POLL;
                end
                else cp_state <= ST_EXEC;
            end

            // ── 条件等待(WAIT 命令 / wait_id):两个状态 = "发起读" + "判结果 & 节流" ──
            // 读回来之后直接进 ST_POLL_WAIT:`poll_gap == 0` 就是"这一拍刚拿到新值"的标记,
            // 所以判等/超时/重读都在同一个状态里,不需要单独的"检查"状态。
            ST_POLL: begin
                scratch2 <= poll_addr;
                mop      <= OP_RD_WORD;
                req_sent <= 1'b0;
                cp_ret_state   <= ST_POLL_WAIT;
                cp_state       <= ST_MEM;
            end
            ST_POLL_WAIT: begin
                timeout_cnt <= timeout_cnt + 32'd1;
                if (poll_gap > 8'h1) begin                  // 节流窗口(8..2):只倒数
                    poll_gap <= poll_gap - 8'h1;
                end
                else if (poll_gap == 8'h1) begin            // 窗口到头 ⇒ 重新读一次(D6)
                    poll_gap <= 8'h0;
                    cp_state <= ST_POLL;
                end
                else if (scratch >= poll_val) begin         // poll_gap==0 = 这一拍刚读到新值
                    timeout_cnt <= 32'h0;
                    cp_state    <= poll_dst_upd ? ST_UPD_FIN : ST_EXEC;
                end
                else if (timeout_hit) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_TIMEOUT;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else begin
                    poll_gap <= 8'd8;                       // 不满足 ⇒ 等 8 拍再重读
                end
            end

            // ── 分派台:这里只"设参数 + 定下一个态",真正的耗时动作都在别的态完成 ────
            // 因此绝大多数分支只有 1~2 行;需要访问内存的命令统一把 cp_ret_state
            // 指向"做完回哪儿",再跳 ST_MEM。
            ST_EXEC: begin
                case (op_q)
                    `CMD_NOP, `CMD_FENCE: cp_state <= ST_UPD_FIN;   // FENCE 由 in-order 完成语义兜住

                    `CMD_WRITE_REG: begin
                        int_wr   <= 1'b1;                 // 直接写寄存器文件(内部口)
                        cp_state <= ST_UPD_FIN;
                    end

                    // READ_REG:读 MMIO 寄存器, 再落到全局内存。
                    `CMD_READ_REG: begin
                        scratch2      <= w3_q;
                        mem_wr_data_q <= int_rdata;
                        mop           <= OP_WR_WORD;
                        req_sent      <= 1'b0;
                        cp_ret_state  <= ST_UPD_FIN;
                        cp_state      <= ST_MEM;
                    end

                    `CMD_SIGNAL: begin          // 往指定地址写一个值(就是一次内存写)
                        scratch2      <= w2_q;
                        mem_wr_data_q <= w3_q;
                        mop           <= OP_WR_WORD;
                        req_sent      <= 1'b0;
                        cp_ret_state  <= ST_UPD_FIN;
                        cp_state      <= ST_MEM;
                    end

                    `CMD_WAIT: begin
                        poll_addr    <= w2_q; 
                        poll_val     <= w3_q;
                        poll_dst_upd <= 1'b1;           // 成功之后去 ST_UPD_FIN
                        poll_gap     <= 8'h0; 
                        timeout_cnt  <= 32'h0;
                        cp_state     <= ST_POLL;
                    end

                    `CMD_IRQ: begin
                        ev_irq_cmd <= 8'h1 << w2_q[2:0];
                        cp_state   <= ST_UPD_FIN;
                    end

                    `CMD_PARAMS: begin
                        params_n   <= w3_q[15:0];
                        params_dst <= `CONST_BASE + {16'h0, w2_q[15:0]};
                        idx        <= 12'd4;
                        cp_state   <= ST_PARAMS;
                    end

                    `CMD_LOAD_KERNEL: begin
                        load_src     <= w2_q;
                        load_words   <= w3_q[13:2];
                        load_sm_mask <= w4_q[1:0];
                        idx          <= 12'h0;
                        crc_init     <= 1'b1;
                        cp_state     <= ST_LOADK;
                    end

                    `CMD_LAUNCH: begin
                        launch_pc_q     <= w2_q;
                        // num_warps ≤ 4 ⇒ 只发 SM0, 拿全部; num_warps ≥ 5 ⇒ 跨 SM,
                        // SM0 拿 wid 0..3、SM1 拿 4..7, 每个 SM 都是 4。
                        launch_warps_q  <= (w3_q > 4'd4) ? 4'd4 : w3_q[3:0];
                        launch_flags_q  <= w4_q[3:0];
                        sm_sel_q        <= sm_sel_c;
                        kernel_active_q <= 1'b1;
                        cp_state        <= ST_LAUNCH;
                    end

                    `CMD_GEMM, `CMD_2D_FILL, `CMD_2D_BLIT, `CMD_2D_DRAW,
                    `CMD_2D_SET_REG, `CMD_2D_FLUSH, 8'h15, 8'h16, 8'h17: begin
                        eng_sel_q    <= dec_engine;     // cmd_decode 已按 opcode 定好引擎
                        eng_hdr_sent <= 1'b0;
                        idx          <= 12'd2;
                        cp_state     <= ST_ENG_SEND;
                    end

                    default: cp_state <= ST_UPD_FIN;          // 厂商自定义(0x80..0xFF):按长度跳过
                endcase
            end

            // PARAMS:逐字写常量区。idx 在发起写时即推进, 完成后直接回来判出口
            ST_PARAMS: begin
                if ({4'h0, idx} >= (16'd4 + params_n)) cp_state <= ST_UPD_FIN;
                else if (idx < 12'd16) begin
                    scratch       <= cmd_w[idx[3:0]];
                    scratch2      <= params_dst + {18'h0, (idx - 12'd4), 2'b00};
                    mem_wr_data_q <= cmd_w[idx[3:0]];
                    mop           <= OP_WR_WORD;
                    req_sent      <= 1'b0;
                    idx           <= idx + 12'h1;
                    cp_ret_state  <= ST_PARAMS;
                    cp_state      <= ST_MEM;
                end
                else if (!line_hit(cmd_addr(idx))) begin
                    scratch2       <= {cmd_addr(idx)[31:6], 6'b0};
                    mop            <= OP_RD_LINE;
                    req_sent       <= 1'b0;
                    cp_ret_state   <= ST_PARAMS;              // 取回行后重来这一步
                    cp_state       <= ST_MEM;
                end
                else begin
                    scratch       <= line_word(cmd_addr(idx));
                    scratch2      <= params_dst + {18'h0, (idx - 12'd4), 2'b00};
                    mem_wr_data_q <= line_word(cmd_addr(idx));
                    mop           <= OP_WR_WORD;
                    req_sent      <= 1'b0;
                    idx           <= idx + 12'h1;
                    cp_ret_state  <= ST_PARAMS;
                    cp_state      <= ST_MEM;
                end
            end

            // LOAD_KERNEL:流式读镜像 → 写 SRAM + CRC
            ST_LOADK: begin
                // 逐字:全局内存读镜像 → 写指令 SRAM(按 sm_mask 广播)→ 顺带算 CRC
                if (idx >= load_words) cp_state <= ST_LOADK_ND;
                else if (!line_hit(load_word_addr)) begin
                    scratch2       <= {load_word_addr[31:6], 6'b0};
                    mop            <= OP_RD_LINE;
                    req_sent       <= 1'b0;
                    cp_ret_state   <= ST_LOADK;
                    cp_state       <= ST_MEM;
                end
                else begin
                    scratch     <= line_word(load_word_addr);
                    sram_data_q <= line_word(load_word_addr);
                    sram_addr_q <= idx;                    // SRAM 字地址 = 镜像内字序号
                    sram_en_q   <= 1'b1;
                    sram_last_q <= (idx == (load_words - 12'd1));
                    if (idx < 12'd8) img_hdr[idx[2:0]] <= line_word(load_word_addr);
                    // CRC 只算指令区:从 entry_off 起。注意 idx<2 时 img_hdr[1] 还没写回来
                    // (它是寄存器), 那两个字的条件会误判 ⇒ 显式排除(entry_off ≥ 32 见 §6.4)。
                    if ((idx >= 12'd2) && (({20'h0, idx} * 32'd4) >= img_hdr[1]))
                        crc_en <= 1'b1;
                    idx <= idx + 12'h1;
                end
            end
            ST_LOADK_ND: begin
                // 等 SRAM 的 load_done, 并校验镜像头
                if (|sram_err_ovf) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_CMD;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else if (!(|(sram_load_done & load_sm_mask))) begin
                    cp_state <= ST_LOADK_ND;                   // 再等一拍
                end
                else if ((img_hdr[0] != IMG_MAGIC) ||
                         (img_hdr[6] != 32'h0) || (img_hdr[7] != 32'h0) ||
                         (img_hdr[2] != w3_q) ||
                         (img_hdr[1][1:0] != 2'b00) || (img_hdr[1] < IMG_HDR_BYTES) ||
                         (img_hdr[1] > img_hdr[2]) ||
                         (img_hdr[5] != (img_hdr[2] - img_hdr[1])) ||
                         (crc_val != img_hdr[4])) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_CMD;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else begin
                    img_we      <= 1'b1;
                    img_sm_mask <= load_sm_mask;
                    cp_state    <= ST_UPD_FIN;
                end
            end

            // LAUNCH:分发脉冲(ST_LAUNCH 那一拍 sm_launch_valid 已由组合逻辑给出)
            // → 清零超时计数 → 等涉及的 SM 全 DONE。
            ST_LAUNCH: begin
                timeout_cnt <= 32'h0;
                cp_state    <= ST_WAIT_SM;
            end
            // 完成判据 (sm_done & sm_sel_q) == sm_sel_q:既要求"选中的 SM 都置了
            // done",又自动忽略没参与本次 kernel 的那个 SM 的残留 done。
            ST_WAIT_SM: begin
                timeout_cnt <= timeout_cnt + 32'd1;
                if (sm_err_sel != 4'h0) begin            // 任一涉及 SM 出错 ⇒ kernel 失败
                    cp_err_we       <= 1'b1; 
                    cp_err_we_data  <= CPERR_SM_FAULT;
                    ev_cp_error     <= 1'b1;
                    kernel_active_q <= 1'b0;
                    cp_state        <= ST_ERR;                        // 不写信号量(§4.3)
                end
                else if ((sm_done & sm_sel_q) == sm_sel_q) begin
                    ev_launch_done  <= 1'b1;
                    kernel_active_q <= 1'b0;
                    timeout_cnt     <= 32'h0;
                    cp_state        <= ST_UPD_FIN;
                end
                else if (timeout_hit) begin
                    cp_err_we       <= 1'b1; 
                    cp_err_we_data  <= CPERR_TIMEOUT;
                    ev_cp_error     <= 1'b1;
                    kernel_active_q <= 1'b0;
                    cp_state        <= ST_ERR;
                end
            end

            // 引擎:两段式 —— 先推命令头(eng_valid 一拍),再逐字推 payload。
            // eng_ready 为高才前进,所以引擎忙时 CP 停在这里背压(不排队、不丢命令)。
            ST_ENG_SEND: begin
                if (!eng_hdr_sent) begin
                    if (eng_ready) begin
                        if (payload_len == 5'd0) begin          // 没有 payload(如 2D_FLUSH)
                            cp_state <= ST_WAIT_ENG; timeout_cnt <= 32'h0;
                        end
                        else begin
                            eng_hdr_sent <= 1'b1;
                            idx          <= 12'h0;
                        end
                    end
                end
                else if (eng_ready) begin
                    if (idx[4:0] == (payload_len - 5'd1)) begin
                        eng_hdr_sent <= 1'b0;
                        timeout_cnt  <= 32'h0;
                        cp_state     <= ST_WAIT_ENG;
                    end
                    else idx <= idx + 12'h1;
                end
            end
            ST_WAIT_ENG: begin
                timeout_cnt <= timeout_cnt + 32'd1;
                if (eng_status_err) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_ENGINE_FAULT;
                    ev_cp_error    <= 1'b1; 
                    cp_state       <= ST_ERR;
                end
                else if (eng_status_ok) begin
                    if (eng_sel_q == ENG_2D) ev_2d_done   <= 1'b1;
                    else                     ev_gemm_done <= 1'b1;
                    timeout_cnt <= 32'h0;
                    cp_state          <= ST_UPD_FIN;
                end
                else if (timeout_hit) begin
                    cp_err_we      <= 1'b1; 
                    cp_err_we_data <= CPERR_TIMEOUT;
                    ev_cp_error    <= 1'b1;  
                    cp_state       <= ST_ERR;
                end
            end

            // 提交:推进 HEAD(组合脉冲, 见 head_we)、打信号量、置 IRQ
            ST_UPD_FIN: begin
                if (sig_ok_q & ~sig_rd_sent) begin
                    sig_rd_sent  <= 1'b1;
                    scratch2     <= sem_base + {14'h0, sig_q, 2'b00};
                    mop          <= OP_RD_WORD;
                    req_sent     <= 1'b0;
                    cp_ret_state <= ST_SEM_WR;
                    cp_state     <= ST_MEM;
                end
                else begin
                    sig_rd_sent <= 1'b0;
                    if (flags_q[CFLAG_IRQ_ON_DONE]) ev_cmd_done <= 1'b1;
                    line_valid <= 1'b0;
                    if (is_last_cmd) begin
                        doorbell_ack <= 1'b1;            // 队列空便回 IDLE
                        cp_state     <= ST_IDLE;
                    end
                    else begin
                        idx <= 12'h0;
                        cp_state  <= ST_LOADW;
                    end
                end
            end
            // 信号量的读-改-写第二拍:scratch 就是上一步 ST_MEM 读回的 SEM[id]。
            // 只有 CP 会碰信号量(不做 atomics),所以不需要锁或 CAS。
            ST_SEM_WR: begin
                scratch2      <= sem_base + {14'h0, sig_q, 2'b00};
                mem_wr_data_q <= scratch + 32'd1;        // SEM[id] += 1(读-改-写只由 CP 做)
                mop           <= OP_WR_WORD;
                req_sent      <= 1'b0;
                cp_ret_state  <= ST_UPD_FIN;
                cp_state      <= ST_MEM;
            end

            // 内存执行器:整行读走命令缓存;单字读/写走 req → ack → rsp 两段握手
            ST_MEM: begin
                if (mop == OP_RD_LINE) begin
                    cc_req      <= 1'b1;
                    cc_req_line <= scratch2[31:6];
                    if (cc_rsp) begin
                        cc_req <= 1'b0;
                        mop    <= OP_NONE;
                        if (cc_rsp_err) begin
                            cp_err_we <= 1'b1; cp_err_we_data <= CPERR_BUS;
                            ev_cp_error <= 1'b1; cp_state <= ST_ERR;
                        end
                        else begin
                            for (int w = 0; w < LINE_WORDS; w++)
                                line_buf[w] <= cc_rsp_data[w*32 +: 32];
                            line_base  <= cc_rsp_line;
                            line_valid <= 1'b1;
                            cp_state   <= cp_ret_state;
                        end
                    end
                end
                else if (!req_sent) begin
                    if (mem_ack) req_sent <= 1'b1;      // ack 之后 req 才会撤销
                end
                else if (mop == OP_RD_WORD) begin
                    if (mem_vld) begin                  // 单字读只有一拍
                        req_sent <= 1'b0;
                        mop      <= OP_NONE;
                        if (mem_err) begin
                            cp_err_we <= 1'b1; cp_err_we_data <= CPERR_BUS;
                            ev_cp_error <= 1'b1; cp_state <= ST_ERR;
                        end
                        else begin
                            scratch  <= mem_word;
                            cp_state <= cp_ret_state;
                        end
                    end
                end
                else if (mem_wr_rsp) begin
                    req_sent <= 1'b0;
                    mop      <= OP_NONE;
                    if (mem_err) begin
                        cp_err_we <= 1'b1; cp_err_we_data <= CPERR_BUS;
                        ev_cp_error <= 1'b1; cp_state <= ST_ERR;
                    end
                    else begin
                        cp_state <= cp_ret_state;       // 写操作不需要数据回填
                    end
                end
            end

            default: cp_state <= ST_IDLE;
            endcase
        end
    end
endmodule

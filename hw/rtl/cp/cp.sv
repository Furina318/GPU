`include "defines.vh"

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
    input  wire         mem_rd_rsp     ,
    input  wire [LINE_WORDS*32-1:0] mem_rd_data,
    input  wire         mem_rd_err     ,
    output logic        mem_wr_req     ,
    output logic [31:0] mem_wr_addr    ,
    output logic [31:0] mem_wr_data    ,
    output logic [ 3:0] mem_wr_strb    ,
    input  wire         mem_wr_ack     ,
    input  wire         mem_wr_rsp     ,
    input  wire         mem_wr_err     ,

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
    // 微操作(内存访问)
    typedef enum logic [1:0] { OP_NONE=0, OP_RD_LINE=1, OP_RD_WORD=2, OP_WR_WORD=3 } mem_op_t;

    typedef enum logic [4:0] {
        ST_IDLE     , ST_LOADW  , ST_LENCHK    , ST_LOADW2     , ST_DECODE   ,
        ST_EXEC     , ST_MEM    , ST_PARAMS    , ST_PARAMS_RET , ST_LOADK    ,
        ST_LOADK_ND , ST_LAUNCH , ST_WAIT_SM   , ST_ENG_SEND   , ST_WAIT_ENG ,
        ST_SEM_WR   , ST_POLL   , ST_POLL_WAIT , ST_UPD        , ST_UPD_FIN  ,
        ST_ERR
    } cp_state_t;

    cp_state_t st, ret_st;
    mem_op_t   mop;
    logic      req_sent;

    // 命令头与操作数
    logic [15:0] len_q;
    logic [ 7:0] flags_q, op_q;
    logic [15:0] sig_q, wait_q;
    logic [31:0] cmd_w [0:15];
    logic [11:0] idx;              // 通用循环下标(PARAMS ≤ 260 / LOAD_KERNEL ≤ 4096)
    logic [31:0] scratch, scratch2, mem_wr_data_q;
    logic [31:0] w2_q, w3_q;
    logic [ 3:0] w4_q;              // LOAD_KERNEL 的 sm_mask / LAUNCH 的 flags
    logic        sig_ok_q;

    // 命令级暂存
    logic [15:0] params_n;
    logic [31:0] params_dst;
    logic [31:0] load_src;
    logic [11:0] load_words;        // 镜像字数(≤ 4096)
    logic [31:0] img_hdr [0:7];
    logic [ 1:0] load_sm_mask;
    logic [ 3:0] launch_warps_q;
    logic [ 3:0] launch_flags_q;
    logic [31:0] launch_pc_q;
    logic [ 1:0] sm_sel_q, eng_sel_q;
    logic [31:0] poll_addr, poll_val;
    logic        poll_dst_upd;          // 轮询成功后去哪:0=ST_EXEC 1=ST_UPD
    logic [ 7:0]  poll_gap;
    logic [31:0] timeout_cnt;
    logic        kernel_active_q;
    logic        eng_hdr_sent;

    // 行缓冲
    logic [31:0] line_buf [0:LINE_WORDS-1];
    logic [31:6] line_base;
    logic        line_valid;

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
    `UNUSED_VAR(doorbell_evt)           // 只需要 pending 位(cp_regs 内部维护)
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

    // 时间/状态组合量
    wire queued  = (head != tail);
    wire working = (st != ST_IDLE);
    wire busy_i  = queued | working;
    wire idle_i  = ~queued & ~working & (cp_error == CPERR_NONE);

    wire [31:0] next_head_raw = head + {16'h0, len_q} * 32'd4;
    wire [31:0] next_head     = (next_head_raw >= queue_base + queue_size)
                                ? (next_head_raw - queue_size) : next_head_raw;
    wire        is_last_cmd   = (next_head_raw == tail);

    wire [3:0] sm_err_sel = (sm_sel_q[0] ? sm0_error : 4'h0) |
                            (sm_sel_q[1] ? sm1_error : 4'h0);
    wire [2:0] eng_status_sel = (eng_sel_q == ENG_2D) ? twod_status : gemm_status;
    wire       eng_status_err = eng_status_sel[ENG_ST_ERROR];
    wire       eng_status_ok  = eng_status_sel[ENG_ST_DONE] && !eng_status_sel[ENG_ST_BUSY];
    wire       timeout_hit    = (timeout_cnt >= timeout_cycles);

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

    // LAUNCH 前置条件
    function automatic logic [3:0] launch_check(
        input logic [31:0] kpc, input logic [3:0] nw, input logic [3:0] lflags,
        input logic [1:0] ssel
    );
        logic [1:0] inv;
        inv = ssel;
        if (inv[0] && (sm0_error != 4'h0)) return CPERR_STALE_SM_ERROR;   // #2
        if (inv[1] && (sm1_error != 4'h0)) return CPERR_STALE_SM_ERROR;
        if (kpc[1:0] != 2'b00)             return CPERR_CMD;              // #4 对齐
        if (inv[0] && (kpc >= img_size[0])) return CPERR_CMD;             // #4 范围
        if (inv[1] && (kpc >= img_size[1])) return CPERR_CMD;
        if ((kernel_mask & inv) != inv)     return CPERR_CMD;             // #5 镜像已加载
        if (inv[0] && (img_flags[0][3:0] != lflags)) return CPERR_CMD;    // #6 flags 交叉核对
        if (inv[1] && (img_flags[1][3:0] != lflags)) return CPERR_CMD;
        if ((lflags[FLAG_USES_BARRIER] | lflags[FLAG_USES_SHARED] |
             lflags[FLAG_REQUIRES_SINGLE_SM]) && (nw > 4'd4))
            return CPERR_LAUNCH_REJECT;                                   // #7
        return CPERR_NONE;
    endfunction

    // WRITE_REG 的额外校验
    wire wr_que_base = (w2_q == {24'h0, `MMIO_CMD_QUEUE_BASE});
    wire wr_que_size = (w2_q == {24'h0, `MMIO_CMD_QUEUE_SIZE});
    wire que_base_val_ok = (w3_q >= `MMIO_END) && (w3_q[1:0] == 2'b00);
    wire que_size_val_ok = (w3_q >= 32'd8) && (w3_q[2:0] == 3'b000);
    wire que_cfg_state_ok = is_last_cmd & ~kernel_active_q;
    wire wr_reg_bad_state = (wr_que_base | wr_que_size) & ~que_cfg_state_ok;
    wire wr_reg_bad_value = (wr_que_base & ~que_base_val_ok) |
                            (wr_que_size & ~que_size_val_ok);

    wire [1:0] sm_sel_c = (cmd_w[3][3:0] > 4'd4) ? 2'b11 : 2'b01;
    wire [3:0] launch_err_c = launch_check(cmd_w[2], cmd_w[3][3:0], cmd_w[4][3:0], sm_sel_c);

    axil_slave u_axil (
        .clk(clk), .rst(rst),
        .s_awvalid(s_awvalid), .s_awready(s_awready), .s_awaddr(s_awaddr),
        .s_wvalid(s_wvalid),   .s_wready(s_wready),   .s_wdata(s_wdata), .s_wstrb(s_wstrb),
        .s_bvalid(s_bvalid),   .s_bready(s_bready),   .s_bresp(s_bresp),
        .s_arvalid(s_arvalid), .s_arready(s_arready), .s_araddr(s_araddr),
        .s_rvalid(s_rvalid),   .s_rready(s_rready),   .s_rdata(s_rdata), .s_rresp(s_rresp),
        .reg_wr(ext_wr), .reg_addr(ext_addr), .reg_wdata(ext_wdata),
        .reg_rd(ext_rd), .reg_rdata(regs_ext_rdata)
    );

    logic [7:0] irq_status_w, irq_enable_w;
    cp_regs #(
        .QUEUE_BASE_DEF(QUEUE_BASE_DEF), .QUEUE_SIZE_DEF(QUEUE_SIZE_DEF)
    ) u_regs (
        .clk(clk), .rst(rst),
        .ext_wr(ext_wr), .ext_addr(ext_addr), .ext_wdata(ext_wdata),
        .ext_rd(ext_rd), .ext_rdata(regs_ext_rdata),
        .chk_addr(chk_addr), .chk_wr_ok(chk_wr_ok),
        .cp_reset_evt(cp_reset_evt), .doorbell_evt(doorbell_evt),
        .busy_i(busy_i), .idle_i(idle_i),
        .sm_done_i(sm_done), .sm0_error_i(sm0_error), .sm1_error_i(sm1_error),
        .sm0_fault_pc_i(sm0_fault_pc), .sm1_fault_pc_i(sm1_fault_pc),
        .gemm_status_i(gemm_status), .gemm_error_i(gemm_error),
        .twod_status_i(twod_status), .twod_error_i(twod_error),
        .sm_err_clr(sm_err_clr), .eng_err_clr(eng_err_clr),
        .head_o(head), .tail_o(tail), .queue_base_o(queue_base), .queue_size_o(queue_size),
        .sem_base_o(sem_base), .timeout_o(timeout_cycles),
        .kernel_mask_o(kernel_mask), .img_entry_o(img_entry), .img_size_o(img_size),
        .img_flags_o(img_flags), .cp_error_o(cp_error),
        .irq_status_o(irq_status_w), .irq_enable_o(irq_enable_w),
        .doorbell_pend_o(doorbell_pend),
        .int_addr(int_addr), .int_rdata(int_rdata),
        .int_wr(int_wr), .int_wr_addr(int_wr_addr), .int_wr_wdata(int_wr_wdata),
        .head_we(head_we), .head_wdata(head_wdata),
        .tail_we(1'b0), .tail_wdata(32'h0),
        .cp_err_we(cp_err_we), .cp_err_wdata(cp_err_we_data),
        .irq_set(irq_set), .doorbell_ack(doorbell_ack),
        .img_we(img_we), .img_sm_mask(img_sm_mask),
        .img_entry_w(img_entry_w), .img_size_w(img_size_w), .img_flags_w(img_flags_w),
        .irq_o(irq)
    );

    // IRQ_STATUS/IRQ_ENABLE 只在 MMIO 读口用得上;这里留两个观测点(波形/调试)
    `UNUSED_VAR(irq_status_w)
    `UNUSED_VAR(irq_enable_w)

    logic       dec_ok, dec_is_engine, dec_is_vendor;
    logic [3:0] dec_err;
    logic [1:0] dec_engine;
    cmd_decode u_dec (
        .len(len_q), .flags(flags_q[7:2]), .opcode(op_q), .signal_id(sig_q), .wait_id(wait_q),
        .cmd_w(cmd_w),
        .ok(dec_ok), .err(dec_err), .is_engine(dec_is_engine), .engine(dec_engine),
        .is_vendor(dec_is_vendor)
    );
    `UNUSED_VAR(dec_is_engine)          // 引擎分派在 ST_EXEC 里按 opcode 判, 这两个留作端口语义
    `UNUSED_VAR(dec_is_vendor)
    
    logic        crc_init, crc_en;
    logic [31:0] crc_val;
    crc32_word u_crc (
        .clk(clk), .rst(rst), .init(crc_init), .en(crc_en), .data(scratch), .crc(crc_val)
    );

    // 内存请求/响应
    wire op_is_rd = (mop == OP_RD_LINE) || (mop == OP_RD_WORD);
    assign mem_rd_req  = op_is_rd & ~req_sent;
    assign mem_rd_addr = scratch2;
    assign mem_rd_line = (mop == OP_RD_LINE);
    assign mem_wr_req  = (mop == OP_WR_WORD) & ~req_sent;
    assign mem_wr_addr = scratch2;
    assign mem_wr_data = mem_wr_data_q;
    assign mem_wr_strb = 4'hF;

    wire mem_ack = op_is_rd ? mem_rd_ack : mem_wr_ack;
    wire mem_rsp = op_is_rd ? mem_rd_rsp : mem_wr_rsp;
    wire mem_err = op_is_rd ? mem_rd_err : mem_wr_err;

    // 指令 SRAM 预加载(广播到 sm_mask 选中的 SM)
    logic sram_en_q, sram_last_q;
    logic [11:0] sram_addr_q;
    logic [31:0] sram_data_q;
    assign sram_load_en   = {2{sram_en_q}}   & load_sm_mask;
    assign sram_load_addr = {1'b0, sram_addr_q};   // instr_mem 的 13 位口:最高位判越界
    assign sram_load_data = sram_data_q;
    assign sram_load_last = {2{sram_last_q}} & load_sm_mask;

    // 引擎 payload 流
    assign eng_valid  = (st == ST_ENG_SEND) & ~eng_hdr_sent;
    assign eng_engine = eng_sel_q;
    assign eng_opcode = op_q;
    assign eng_len    = len_q;
    wire [4:0] payload_len = len_q[4:0] - 5'd2;      // payload = word2..word_{len-1}
    assign eng_wvalid = (st == ST_ENG_SEND) & eng_hdr_sent & (idx[4:0] < payload_len);
    assign eng_wdata  = cmd_w[idx[3:0] + 4'd2];      // 最多 14 个字(GEMM 是 16 字命令)
    assign eng_wlast  = eng_wvalid & (idx[4:0] == (payload_len - 5'd1));

    // SM 分发
    assign sm_launch_valid = (st == ST_LAUNCH) ? sm_sel_q : 2'b00;
    assign sm_launch_pc    = launch_pc_q;
    assign sm_launch_warps = launch_warps_q;
    assign sm_launch_flags = launch_flags_q;

    // HEAD 的推进用组合脉冲:在 ST_UPD 那一拍就落到寄存器上,
    // 这样下一拍(ST_UPD_FIN)判"队列是否空"看到的是新值 —— 否则会把同一条命令反复取。
    assign head_we    = (st == ST_UPD);
    assign head_wdata = next_head;
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

    // 主状态机
    always_ff @(posedge clk) begin
        if (rst) begin
            st <= ST_IDLE; ret_st <= ST_IDLE; mop <= OP_NONE; req_sent <= 1'b0;
            len_q <= 16'h0; flags_q <= 8'h0; op_q <= 8'h0;
            sig_q <= SEM_NONE; wait_q <= SEM_NONE;
            idx <= 12'h0; scratch <= 32'h0; scratch2 <= 32'h0; mem_wr_data_q <= 32'h0;
            w2_q <= 32'h0; w3_q <= 32'h0; w4_q <= 4'h0;
            sig_ok_q <= 1'b0;
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
            int_wr      <= 1'b0;
            img_we      <= 1'b0;
            cp_err_we   <= 1'b0;
            doorbell_ack<= 1'b0;
            crc_init    <= 1'b0;
            crc_en      <= 1'b0;
            sram_en_q   <= 1'b0;
            sram_last_q <= 1'b0;
            ev_irq_cmd  <= 8'h0;
            ev_cp_error <= 1'b0;
            ev_cmd_done <= 1'b0;
            ev_launch_done <= 1'b0;
            ev_2d_done  <= 1'b0;
            ev_gemm_done<= 1'b0;

            if (cp_reset_evt) begin              
                st <= ST_IDLE; mop <= OP_NONE; req_sent <= 1'b0;
                line_valid <= 1'b0; kernel_active_q <= 1'b0; timeout_cnt <= 32'h0;
            end
            else if (st == ST_ERR) begin
                st <= ST_ERR;                       // 停机:等 host 写 CP_RESET
            end
            else case (st)

            ST_IDLE: begin
                if (doorbell_pend) begin
                    if (head == tail) begin
                        doorbell_ack <= 1'b1;       // 空队列敲门:直接消费掉
                    end
                    else begin
                        idx         <= 12'h0; 
                        timeout_cnt <= 32'h0; 
                        st          <= ST_LOADW;
                    end
                end
            end

            ST_LOADW: begin
                if (!line_hit(cmd_addr(idx))) begin
                    scratch2 <= {cmd_addr(idx)[31:6], 6'b0};
                    mop      <= OP_RD_LINE;
                    req_sent <= 1'b0;
                    ret_st   <= ST_LOADW;
                    st       <= ST_MEM;
                end
                else begin
                    cmd_w[idx[3:0]] <= line_word(cmd_addr(idx));
                    if (idx == 12'h0) begin
                        len_q   <= line_word(cmd_addr(idx))[31:16];
                        flags_q <= line_word(cmd_addr(idx))[15:8];
                        op_q    <= line_word(cmd_addr(idx))[7:0];
                    end
                    else if (idx == 12'h1) begin
                        sig_q  <= line_word(cmd_addr(idx))[31:16];
                        wait_q <= line_word(cmd_addr(idx))[15:0];
                    end
                    if (idx == 12'h1) st <= ST_LENCHK;
                    else             idx <= 12'h1;
                end
            end

            // 长度与队列边界
            ST_LENCHK: begin
                if (len_q < 16'd2) begin
                    cp_err_we <= 1'b1;
                    cp_err_we_data <= CPERR_CMD;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if (next_head_raw > queue_base + queue_size) begin
                    cp_err_we <= 1'b1; 
                    cp_err_we_data <= CPERR_CMD;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if ((tail >= head) && (next_head_raw > tail)) begin
                    cp_err_we <= 1'b1; 
                    cp_err_we_data <= CPERR_CMD;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else begin
                    idx       <= 12'h2;
                    sig_ok_q  <= (sig_q != SEM_NONE);
                    st        <= ST_LOADW2;
                end
            end

            // 读命令体(最多 16 字;PARAMS 更长的部分在后面流式读)
            ST_LOADW2: begin
                if ((idx >= 12'd16) || ({4'h0, idx} >= len_q)) begin
                    w2_q <= cmd_w[4'd2]; w3_q <= cmd_w[4'd3]; w4_q <= cmd_w[4'd4][3:0];
                    st   <= ST_DECODE;
                end
                else if (!line_hit(cmd_addr(idx))) begin
                    scratch2 <= {cmd_addr(idx)[31:6], 6'b0};
                    mop      <= OP_RD_LINE;
                    req_sent <= 1'b0;
                    ret_st   <= ST_LOADW2;
                    st       <= ST_MEM;
                end
                else begin
                    cmd_w[idx[3:0]] <= line_word(cmd_addr(idx));
                    idx        <= idx + 12'h1;
                end
            end

            // 校验
            ST_DECODE: begin
                if (!dec_ok) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= dec_err;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if ((op_q == `CMD_WRITE_REG) && wr_reg_bad_value) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_CMD;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if ((op_q == `CMD_WRITE_REG) && wr_reg_bad_state) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_ILLEGAL_STATE;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if ((op_q == `CMD_WRITE_REG) && !chk_wr_ok) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_CMD;   // 只读寄存器的写只允许被忽略
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if ((op_q == `CMD_LOAD_KERNEL) && kernel_active_q) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_ILLEGAL_STATE;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if ((op_q == `CMD_LAUNCH) && (launch_err_c != CPERR_NONE)) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= launch_err_c;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if (wait_q != SEM_NONE) begin
                    poll_addr <= sem_base + {14'h0, wait_q, 2'b00};
                    poll_val  <= 32'd1;                 // wait_id 满足 = SEM[id] != 0
                    poll_dst_upd <= 1'b0;               // 成功之后去 ST_EXEC
                    poll_gap  <= 8'h0;
                    timeout_cnt <= 32'h0;
                    st <= ST_POLL;
                end
                else st <= ST_EXEC;
            end

            // ── 条件等待(WAIT 命令 / wait_id):两个状态 = "发起读" + "判结果 & 节流" ──
            // 读回来之后直接进 ST_POLL_WAIT:`poll_gap == 0` 就是"这一拍刚拿到新值"的标记,
            // 所以判等/超时/重读都在同一个状态里,不需要单独的"检查"状态。
            ST_POLL: begin
                scratch2 <= poll_addr;
                mop      <= OP_RD_WORD;
                req_sent <= 1'b0;
                ret_st   <= ST_POLL_WAIT;
                st       <= ST_MEM;
            end
            ST_POLL_WAIT: begin
                timeout_cnt <= timeout_cnt + 32'd1;
                if (poll_gap > 8'h1) begin                  // 节流窗口(8..2):只倒数
                    poll_gap <= poll_gap - 8'h1;
                end
                else if (poll_gap == 8'h1) begin            // 窗口到头 ⇒ 重新读一次(D6)
                    poll_gap <= 8'h0;
                    st       <= ST_POLL;
                end
                else if (scratch >= poll_val) begin         // poll_gap==0 = 这一拍刚读到新值
                    timeout_cnt <= 32'h0;
                    st <= poll_dst_upd ? ST_UPD : ST_EXEC;
                end
                else if (timeout_hit) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_TIMEOUT;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else begin
                    poll_gap <= 8'd8;                       // 不满足 ⇒ 等 8 拍再重读
                end
            end

            ST_EXEC: begin
                case (op_q)
                    `CMD_NOP, `CMD_FENCE: st <= ST_UPD;

                    `CMD_WRITE_REG: begin
                        int_wr <= 1'b1;                 // 直接写寄存器文件(内部口)
                        st     <= ST_UPD;
                    end

                    // READ_REG:读 MMIO 寄存器, 再落到全局内存。寄存器文件是**组合读**(`int_addr = w2_q`),
                    // 所以这一拍就能拿到值, 不需要额外的"读寄存器"状态。
                    `CMD_READ_REG: begin
                        scratch       <= int_rdata;      // 只进波形/调试, 后续不再使用
                        scratch2      <= w3_q;
                        mem_wr_data_q <= int_rdata;
                        mop           <= OP_WR_WORD;
                        req_sent      <= 1'b0;
                        ret_st        <= ST_UPD;
                        st            <= ST_MEM;
                    end

                    `CMD_SIGNAL: begin
                        scratch2      <= w2_q;
                        mem_wr_data_q <= w3_q;
                        mop           <= OP_WR_WORD;
                        req_sent      <= 1'b0;
                        ret_st        <= ST_UPD;
                        st            <= ST_MEM;
                    end

                    `CMD_WAIT: begin
                        poll_addr <= w2_q; poll_val <= w3_q;
                        poll_dst_upd <= 1'b1;           // 成功之后去 ST_UPD
                        poll_gap <= 8'h0; timeout_cnt <= 32'h0;
                        st <= ST_POLL;
                    end

                    `CMD_IRQ: begin
                        ev_irq_cmd <= 8'h1 << w2_q[2:0];
                        st <= ST_UPD;
                    end

                    `CMD_PARAMS: begin
                        params_n   <= w3_q[15:0];
                        params_dst <= `CONST_BASE + {16'h0, w2_q[15:0]};
                        idx        <= 12'd4;
                        st         <= ST_PARAMS;
                    end

                    `CMD_LOAD_KERNEL: begin
                        load_src     <= w2_q;
                        load_words   <= w3_q[13:2];
                        load_sm_mask <= w4_q[1:0];
                        idx          <= 12'h0;
                        crc_init     <= 1'b1;
                        st           <= ST_LOADK;
                    end

                    `CMD_LAUNCH: begin
                        launch_pc_q     <= w2_q;
                        launch_warps_q  <= w3_q[3:0];
                        launch_flags_q  <= w4_q[3:0];
                        sm_sel_q        <= sm_sel_c;
                        kernel_active_q <= 1'b1;
                        st              <= ST_LAUNCH;
                    end

                    `CMD_GEMM, `CMD_2D_FILL, `CMD_2D_BLIT, `CMD_2D_DRAW,
                    `CMD_2D_SET_REG, `CMD_2D_FLUSH, 8'h15, 8'h16, 8'h17: begin
                        eng_sel_q    <= dec_engine;     // cmd_decode 已按 opcode 定好引擎
                        eng_hdr_sent <= 1'b0;
                        idx          <= 12'd2;
                        st           <= ST_ENG_SEND;
                    end

                    default: st <= ST_UPD;              // 厂商自定义(0x80..0xFF):按长度跳过
                endcase
            end

            // PARAMS:逐字写常量区
            ST_PARAMS: begin
                if ({4'h0, idx} >= (16'd4 + params_n)) st <= ST_UPD;
                else if (idx < 12'd16) begin
                    scratch  <= cmd_w[idx[3:0]];
                    scratch2 <= params_dst + {18'h0, (idx - 12'd4), 2'b00};
                    mem_wr_data_q <= cmd_w[idx[3:0]];
                    mop      <= OP_WR_WORD;
                    req_sent <= 1'b0;
                    ret_st   <= ST_PARAMS_RET;
                    st       <= ST_MEM;
                end
                else if (!line_hit(cmd_addr(idx))) begin
                    scratch2 <= {cmd_addr(idx)[31:6], 6'b0};
                    mop      <= OP_RD_LINE;
                    req_sent <= 1'b0;
                    ret_st   <= ST_PARAMS;              // 取回行后重来这一步
                    st       <= ST_MEM;
                end
                else begin
                    scratch  <= line_word(cmd_addr(idx));
                    scratch2 <= params_dst + {18'h0, (idx - 12'd4), 2'b00};
                    mem_wr_data_q <= line_word(cmd_addr(idx));
                    mop      <= OP_WR_WORD;
                    req_sent <= 1'b0;
                    ret_st   <= ST_PARAMS_RET;
                    st       <= ST_MEM;
                end
            end
            ST_PARAMS_RET: begin
                idx <= idx + 12'h1;
                st  <= ST_PARAMS;
            end

            // LOAD_KERNEL:流式读镜像 → 写 SRAM + CRC
            ST_LOADK: begin
                // 逐字:全局内存读镜像 → 写指令 SRAM(按 sm_mask 广播)→ 顺带算 CRC
                if (idx >= load_words) st <= ST_LOADK_ND;
                else if (!line_hit(load_word_addr)) begin
                    scratch2 <= {load_word_addr[31:6], 6'b0};
                    mop      <= OP_RD_LINE;
                    req_sent <= 1'b0;
                    ret_st   <= ST_LOADK;
                    st       <= ST_MEM;
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
                // 等 SRAM 的 load_done, 并校验镜像头(§6.4)
                if (|sram_err_ovf) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_CMD;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if (!(|(sram_load_done & load_sm_mask))) begin
                    st <= ST_LOADK_ND;                   // 再等一拍
                end
                else if ((img_hdr[0] != IMG_MAGIC) ||
                         (img_hdr[6] != 32'h0) || (img_hdr[7] != 32'h0) ||
                         (img_hdr[2] != w3_q) ||
                         (img_hdr[1][1:0] != 2'b00) || (img_hdr[1] < IMG_HDR_BYTES) ||
                         (img_hdr[1] > img_hdr[2]) ||
                         (img_hdr[5] != (img_hdr[2] - img_hdr[1])) ||
                         (crc_val != img_hdr[4])) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_CMD;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else begin
                    img_we      <= 1'b1;
                    img_sm_mask <= load_sm_mask;
                    st          <= ST_UPD;
                end
            end

            // LAUNCH:分发脉冲 → 等涉及的 SM 全 DONE
            ST_LAUNCH: begin
                timeout_cnt <= 32'h0;
                st          <= ST_WAIT_SM;
            end
            ST_WAIT_SM: begin
                timeout_cnt <= timeout_cnt + 32'd1;
                if (sm_err_sel != 4'h0) begin            // 任一涉及 SM 出错 ⇒ kernel 失败
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_SM_FAULT;
                    ev_cp_error <= 1'b1;
                    kernel_active_q <= 1'b0;
                    st <= ST_ERR;                        // 不写信号量(§4.3)
                end
                else if ((sm_done & sm_sel_q) == sm_sel_q) begin
                    ev_launch_done  <= 1'b1;
                    kernel_active_q <= 1'b0;
                    timeout_cnt     <= 32'h0;
                    st              <= ST_UPD;
                end
                else if (timeout_hit) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_TIMEOUT;
                    ev_cp_error <= 1'b1;
                    kernel_active_q <= 1'b0;
                    st <= ST_ERR;
                end
            end

            // 引擎:推 payload → 等完成
            ST_ENG_SEND: begin
                if (!eng_hdr_sent) begin
                    if (eng_ready) begin
                        if (payload_len == 5'd0) begin          // 没有 payload(如 2D_FLUSH)
                            st <= ST_WAIT_ENG; timeout_cnt <= 32'h0;
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
                        st           <= ST_WAIT_ENG;
                    end
                    else idx <= idx + 12'h1;
                end
            end
            ST_WAIT_ENG: begin
                timeout_cnt <= timeout_cnt + 32'd1;
                if (eng_status_err) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_ENGINE_FAULT;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
                else if (eng_status_ok) begin
                    if (eng_sel_q == ENG_2D) ev_2d_done   <= 1'b1;
                    else                     ev_gemm_done <= 1'b1;
                    timeout_cnt <= 32'h0;
                    st          <= ST_UPD;
                end
                else if (timeout_hit) begin
                    cp_err_we <= 1'b1; cp_err_we_data <= CPERR_TIMEOUT;
                    ev_cp_error <= 1'b1; st <= ST_ERR;
                end
            end

            // 提交:推进 HEAD、打信号量、置 IRQ
            ST_UPD: begin
                if (sig_ok_q) begin
                    scratch2 <= sem_base + {14'h0, sig_q, 2'b00};
                    mop      <= OP_RD_WORD;
                    req_sent <= 1'b0;
                    ret_st   <= ST_SEM_WR;
                    st       <= ST_MEM;
                end
                else st <= ST_UPD_FIN;
            end
            ST_SEM_WR: begin
                scratch2      <= sem_base + {14'h0, sig_q, 2'b00};
                mem_wr_data_q <= scratch + 32'd1;        // SEM[id] += 1(读-改-写只由 CP 做)
                mop           <= OP_WR_WORD;
                req_sent      <= 1'b0;
                ret_st        <= ST_UPD_FIN;
                st            <= ST_MEM;
            end
            ST_UPD_FIN: begin
                if (flags_q[CFLAG_IRQ_ON_DONE]) ev_cmd_done <= 1'b1;
                line_valid <= 1'b0;
                if (head == tail) begin
                    doorbell_ack <= 1'b1;                // 队列空便回 IDLE
                    st <= ST_IDLE;
                end
                else begin
                    idx <= 12'h0;
                    st  <= ST_LOADW;
                end
            end

            // 内存微操作等待
            ST_MEM: begin
                if (!req_sent) begin
                    if (mem_ack) req_sent <= 1'b1;
                end
                else if (mem_rsp) begin
                    req_sent <= 1'b0;
                    mop      <= OP_NONE;
                    if (mem_err) begin
                        cp_err_we <= 1'b1; cp_err_we_data <= CPERR_BUS;
                        ev_cp_error <= 1'b1; st <= ST_ERR;
                    end
                    else begin
                        if (op_is_rd) begin
                            if (mop == OP_RD_LINE) begin
                                for (int w = 0; w < LINE_WORDS; w++)
                                    line_buf[w] <= mem_rd_data[w*32 +: 32];
                                line_base  <= scratch2[31:6];
                                line_valid <= 1'b1;
                            end
                            else scratch <= mem_rd_data[31:0];
                        end
                        st <= ret_st;
                    end
                end
            end

            default: st <= ST_IDLE;
            endcase
        end
    end
endmodule

`include "defines.vh"

// ============================================================================
//  lsu —— 每 warp 一个的访存单元(ISA §3.2, MEM_LSU.md §4.3)
//
//  一条访存指令的生命周期:
//    ① 地址/对齐检查(lane 级): `addr[1:0] != 0` ⇒ BAD_ALIGN;
//    ② 按 `.s` / `.g` 分派:
//       `.s` 走共享内存端口(共享地址是独立的 16KB 空间,越界由 shared_mem 报);
//       `.g` 先经 mem_map 判区域(MMIO/未映射 ⇒ 各自错误码),再按 32B 行合并后
//           逐个行请求发往 global_mem(WCB 写合并 + AXI);
//    ③ 读数据按 lane 回填,连同 `rd`/`pc`/`inst` 一起交给 wbu 写寄存器。
//
//  握手风格对齐 mdu.sv: `mem_en` 发起, `mem_ready` 表示当拍可收,
//  完成时 `mem_done` 单拍脉冲,并携带 `done_*` 元数据。
// ============================================================================

module lsu #(
    parameter int LANES = `WARP_SIZE
) (
    input  wire                        clk         ,
    input  wire                        rst         ,

    // ---- 来自 EXU / 寄存器堆 ----
    input  wire                        mem_en      ,   // 本拍有一条访存指令
    input  wire                        mem_we      ,   // 1 = store
    input  wire                        mem_shared  ,   // 1 = `.s`(共享内存)
    input  wire [31:0]                 mem_pc      ,
    input  wire [31:0]                 mem_inst    ,
    input  wire [4:0]                  mem_rd      ,
    input  wire [LANES-1:0]            eff_mask    ,   // active ∩ 谓词
    input  wire [LANES-1:0][31:0]      lane_addr   ,
    input  wire [LANES-1:0][31:0]      lane_wdata  ,
    output logic                       mem_ready   ,

    // ---- 共享内存端口(到 lsu_scheduler) ----
    output logic                       smem_req_valid,
    output logic                       smem_req_we   ,
    output logic [LANES-1:0]           smem_lane_en  ,
    output logic [LANES-1:0][31:0]     smem_lane_addr,
    output logic [LANES-1:0][31:0]     smem_lane_wdata,
    input  wire                        smem_req_ready,
    input  wire                        smem_rsp_valid,
    input  wire [LANES-1:0][31:0]      smem_rsp_data ,
    input  wire [3:0]                  smem_conflict ,
    input  wire                        smem_err_align,
    input  wire                        smem_err_oob  ,

    // ---- 全局内存端口(每拍一个 32B 行请求, 到 lsu_scheduler) ----
    output logic                       gmem_req_valid,
    output logic                       gmem_req_we   ,
    output logic [26:0]                gmem_req_line ,
    output logic [LANES-1:0]           gmem_req_wen  ,   // 行内要写的字
    output logic [LANES-1:0][31:0]     gmem_req_wdata,   // 按**行内字号**排列
    input  wire                        gmem_req_ready,
    input  wire                        gmem_rsp_valid,
    input  wire [LANES-1:0][31:0]      gmem_rsp_data ,
    input  wire                        gmem_rsp_err  ,

    // ---- 完成(交给 wbu) ----
    output logic                       mem_done    ,
    output logic                       mem_done_rd ,
    output logic [4:0]                 mem_done_addr,
    output logic [31:0]                mem_done_pc ,
    output logic [31:0]                mem_done_inst,
    output logic [LANES-1:0]           mem_done_we ,   // 该 lane 有有效结果
    output logic [LANES-1:0][31:0]     mem_done_rdata,

    // ---- 错误上报(SM 错误码见 CMDS §8.2)----
    output logic                       mem_err     ,
    output logic [3:0]                 mem_err_code,
    output logic                       mem_busy    ,
    output logic [3:0]                 mem_conflict   // 共享内存冲突额外拍(计费用)
);

    // ======================================================================
    //  地址检查(组合)
    //
    //  L_IDLE 拍用的是**当拍输入**(mem_en 发起的那一刻), 后几拍用的是锁存值;
    //  否则会拿上一条指令的 shared/we 去判这一条的地址。
    // ======================================================================
    logic [LANES-1:0]        cur_mask;
    logic [LANES-1:0][31:0]  cur_addr;
    logic                    cur_we, cur_shared;

    assign cur_mask   = (st == L_IDLE) ? eff_mask  : mask_q;
    assign cur_addr   = (st == L_IDLE) ? lane_addr : addr_q;
    assign cur_we     = (st == L_IDLE) ? mem_we    : we_q;
    assign cur_shared = (st == L_IDLE) ? mem_shared: shared_q;

    logic [LANES-1:0] bad_align;
    logic [LANES-1:0] bad_shared;      // 共享地址越界
    logic [LANES-1:0] m_region_mmio;
    logic [LANES-1:0] m_region_none;
    logic [LANES-1:0] m_region_wr;     // 该区域可写
    /* verilator lint_off UNUSEDSIGNAL */
    logic [3:0]       region_unused [LANES];  // mem_map 的区域编码本模块用不到
    /* verilator lint_on UNUSEDSIGNAL */

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : g_chk
            assign bad_align[gi]  = cur_mask[gi] && (cur_addr[gi][1:0] != 2'b00);
            // 共享地址空间是独立的 16KB: bit[31:14] 必须为 0
            assign bad_shared[gi] = cur_mask[gi] && (|cur_addr[gi][31:14]);

            mem_map u_map (
                .addr        (cur_addr[gi]   ),
                .is_store    (cur_we         ),
                .region      (region_unused[gi]),
                .writable    (m_region_wr[gi]),
                .err_mmio    (m_region_mmio[gi]),
                .err_no_region(m_region_none[gi])
            );
        end
    endgenerate

    // ======================================================================
    //  合并(.g 路径):8 lane → N 个 32B 行请求
    // ======================================================================
    logic [3:0]                  coal_num;
    logic [LANES-1:0][31:0]      coal_line;
    logic [LANES-1:0][LANES-1:0] coal_lanes;
    logic [LANES-1:0][7:0]       coal_wen;
    logic [LANES-1:0][2:0]       coal_word;      // lane → 行内字号

    lsu_coalescer #(.LANES(LANES)) u_coalescer (
        .lane_en   (eff_mask   ),
        .lane_addr (lane_addr  ),
        .is_store  (mem_we     ),
        .req_num   (coal_num   ),
        .req_line  (coal_line  ),
        .req_lanes (coal_lanes ),
        .req_wen   (coal_wen   ),
        .lane_word (coal_word  )
    );

    // lane 写数据按行内字号重排, 供 WCB 逐字合并。
    // 注意必须只挑"属于当前这一行请求"的 lane(req_idx): 不同行里的 lane 可以共用一个
    // 行内字号(如 24B 步长时 lane0 与 lane4 都在各自行的 word0), 不按行过滤就会把
    // 后面那个 lane 的数据写进前面那一行(实测 gouraud 的输出被 96 字节外的顶点覆盖)。
    logic [LANES-1:0][31:0] coal_wdata;
    always_comb begin : pack_wdata
        integer pi;
        integer li;
        coal_wdata = '0;
        for (pi = 0; pi < LANES; pi = pi + 1)
            for (li = 0; li < LANES; li = li + 1)
                if (coal_lanes[req_idx][li] && (coal_word[li] == pi[2:0]))
                    coal_wdata[pi] = lane_wdata[li];
    end

    // ======================================================================
    //  FSM
    // ======================================================================
    typedef enum logic [2:0] {
        L_IDLE, L_SMEM, L_GM_NEXT, L_GM_REQ, L_GM_WAIT, L_DONE
    } lsu_state_t;

    lsu_state_t st;

    logic [4:0]          req_idx;        // 当前行请求号
    logic                sm_req_sent;    // 共享请求已发出(避免重复接受)
    logic [4:0]          rd_q;
    logic [31:0]         pc_q, inst_q;
    logic [LANES-1:0]    mask_q;
    logic                we_q, shared_q;
    logic [LANES-1:0][31:0] addr_q, wdata_q;
    logic [LANES-1:0][31:0] rdata_acc;   // 读数据累积

    logic                 err_now;
    logic [3:0]           err_code_now;

    always_comb begin : err_pick
        err_now      = 1'b0;
        err_code_now = 4'd0;
        if (|bad_align)          begin err_now = 1'b1; err_code_now = 4'd3; end // BAD_ALIGN
        else if (cur_shared && (|bad_shared))
                                 begin err_now = 1'b1; err_code_now = 4'd7; end // NO_MEM_REGION
        else if (!cur_shared && (|m_region_mmio))
                                 begin err_now = 1'b1; err_code_now = 4'd6; end // MMIO_ACCESS
        else if (!cur_shared && (|m_region_none))
                                 begin err_now = 1'b1; err_code_now = 4'd7; end // NO_MEM_REGION
        else if (!cur_shared && cur_we && (|(cur_mask & ~m_region_wr)))
                                 begin err_now = 1'b1; err_code_now = 4'd7; end // 只读区域写
        else if (cur_shared && smem_err_align)
                                 begin err_now = 1'b1; err_code_now = 4'd3; end
        else if (cur_shared && smem_err_oob)
                                 begin err_now = 1'b1; err_code_now = 4'd7; end
        else if (gmem_rsp_err)   begin err_now = 1'b1; err_code_now = 4'd8; end // INTERNAL(总线错误)
    end

    assign mem_ready = (st == L_IDLE);

    // 共享内存请求(整个 warp 一拍发完, 冲突由 shared_mem 自己多轮处理)
    assign smem_req_valid  = (st == L_SMEM) && !sm_req_sent;
    assign smem_req_we     = we_q;
    assign smem_lane_en    = mask_q;
    assign smem_lane_addr  = addr_q;
    assign smem_lane_wdata = wdata_q;

    // 全局内存请求:一次一个 32B 行
    assign gmem_req_valid  = (st == L_GM_REQ);
    assign gmem_req_we     = we_q;
    assign gmem_req_line   = coal_line[req_idx][31:5];   // 32B 行号
    assign gmem_req_wen    = coal_wen[req_idx];
    assign gmem_req_wdata  = coal_wdata;

    assign mem_done       = (st == L_DONE);
    assign mem_done_rd    = !we_q;      // 1 = 这是一次 load
    assign mem_done_addr  = rd_q;
    assign mem_done_pc    = pc_q;
    assign mem_done_inst  = inst_q;
    assign mem_done_we    = (st == L_DONE) ? (we_q ? mask_q : rdata_vld) : '0;
    assign mem_done_rdata = rdata_acc;
    assign mem_busy       = (st != L_IDLE);
    assign mem_conflict   = smem_conflict;

    logic [LANES-1:0] rdata_vld;         // 哪些 lane 的读数据已经回来

    always_ff @(posedge clk) begin
        if (rst) begin
            st         <= L_IDLE;
            req_idx    <= '0;
            sm_req_sent <= 1'b0;
            rd_q       <= '0;
            pc_q       <= '0;
            inst_q     <= '0;
            mask_q     <= '0;
            we_q       <= 1'b0;
            shared_q   <= 1'b0;
            addr_q     <= '0;
            wdata_q    <= '0;
            rdata_acc  <= '0;
            rdata_vld  <= '0;
            mem_err    <= 1'b0;
            mem_err_code <= 4'd0;
        end else begin
            mem_err <= 1'b0;

            unique case (st)
                L_IDLE: begin
                    if (mem_en) begin
                        rd_q      <= mem_rd;
                        pc_q      <= mem_pc;
                        inst_q    <= mem_inst;
                        mask_q    <= eff_mask;
                        we_q      <= mem_we;
                        shared_q  <= mem_shared;
                        addr_q    <= lane_addr;
                        wdata_q   <= lane_wdata;
                        rdata_acc <= '0;
                        rdata_vld <= '0;
                        req_idx   <= '0;
                        sm_req_sent <= 1'b0;
                        if (err_now) begin
                            mem_err      <= 1'b1;
                            mem_err_code <= err_code_now;
                            st           <= L_DONE;   // 出错也走完成, 由上游转成 SM 错误
                        end else if (mem_shared) begin
                            st <= L_SMEM;
                        end else if (coal_num == 4'd0) begin
                            st <= L_DONE;             // 全 lane 被屏蔽
                        end else begin
                            st <= L_GM_REQ;
                        end
                    end
                end

                L_SMEM: begin
                    if (smem_req_valid && smem_req_ready) sm_req_sent <= 1'b1;
                    if (smem_rsp_valid) begin
                        rdata_acc <= smem_rsp_data;
                        rdata_vld <= mask_q;
                        st        <= L_DONE;
                    end
                end

                L_GM_REQ: begin
                    if (gmem_req_ready) st <= L_GM_WAIT;
                end

                L_GM_WAIT: begin
                    if (gmem_rsp_valid) begin
                        // 该行的 lane 按行内字号取回数据
                        for (int wi = 0; wi < LANES; wi = wi + 1) begin
                            if (coal_lanes[req_idx][wi])
                                rdata_acc[wi] <= gmem_rsp_data[coal_word[wi]];
                        end
                        rdata_vld <= rdata_vld | coal_lanes[req_idx];
                        if (req_idx + 5'd1 >= {1'b0, coal_num})
                            st <= L_DONE;
                        else begin
                            req_idx <= req_idx + 5'd1;
                            st      <= L_GM_REQ;
                        end
                    end
                end

                L_DONE: begin
                    st <= L_IDLE;
                end

                default: st <= L_IDLE;
            endcase
        end
    end

endmodule

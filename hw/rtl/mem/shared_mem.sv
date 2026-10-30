`include "defines.vh"

// ============================================================================
//  shared_mem —— SM 私有共享内存(ISA §3.2 / §8,  MEM_LSU.md §4.2)
//
//  每 SM 16KB, 8 bank, 单端口按 warp 服务:
//    · bank = addr[4:2],  bank 内字号 = addr[13:5];
//    · 所有 lane 同地址  = 同拍广播(1 轮);
//    · 同 bank 异地址    = 冲突, 每轮每 bank 只服务一个地址, 串行化;
//    · 额外拍数 conflict_extra = Σ_b(非空 bank 的不同地址数 − 1) = 轮数 − 1.
//
//  地址是**共享地址空间内的偏移**(0x0000..0x3FFF), 越界与未 4B 对齐分别报
//  err_oob / err_align(ISA §2.6 的架构错误, 由 LSU 翻成错误码)。
//
//  时序: 每轮 1 拍(寄存器堆直读);一次请求从 req_valid 到 rsp_valid 共
//  (轮数 + 1) 拍。若改成 BRAM(1 拍读延迟), 需要给每轮加一拍, 见 MEM_LSU.md §4.2。
// ============================================================================

module shared_mem #(
    parameter int SIZE_BYTES = `SHARED_MEM_SIZE,
    parameter int LANES      = `WARP_SIZE,
    parameter int BANKS      = 8
) (
    input  wire                    clk            ,
    input  wire                    rst            ,
    // 请求 (valid 保持到 rsp_valid)
    input  wire                    req_valid      ,
    input  wire                    req_we         ,   // 1 = 写, 0 = 读
    input  wire [LANES-1:0]        lane_en        ,   // effective_mask
    input  wire [LANES-1:0][31:0]  lane_addr      ,
    input  wire [LANES-1:0][31:0]  lane_wdata     ,
    output wire                    req_ready      ,   // 空闲, 可接收请求
    // 响应
    output wire                    rsp_valid      ,   // 完成脉冲(1 拍)
    output wire [LANES-1:0][31:0]  lane_rdata     ,
    output wire [3:0]              conflict_extra ,   // 轮数 − 1
    // 错误(组合, 与请求同拍有效; 出错时整体不执行)
    output logic                   err_align      ,
    output logic                   err_oob
);

    localparam int WORDS      = SIZE_BYTES / 4;
    localparam int WORDS_BANK = WORDS / BANKS;
    localparam int IDX_W      = $clog2(WORDS_BANK);   // 9

    typedef enum logic [1:0] { S_IDLE, S_ROUND, S_DONE } state_t;
    state_t st, st_n;

    logic [31:0] bank_mem [BANKS][WORDS_BANK];

    // ---- 请求锁存 ----
    logic                   we_q    ;
    logic [LANES-1:0]       pend_q  ;
    logic [LANES-1:0][31:0] addr_q  ;
    logic [LANES-1:0][31:0] wdata_q ;
    logic [3:0]             round_q ;

    // ---- 组合: 每 bank 的最小待服务地址 + 本拍服务掩码 ----
    integer bri, lri, bw, lw;

    logic [31:0]      min_addr [BANKS];
    logic [LANES-1:0] serve;

    // 读回数据 + 状态寄存
    logic [LANES-1:0][31:0] rdata_r;

    // 写数据选择: 同一地址多 lane 写时取**最低 lane 号**的数据(确定性语义)
    logic [31:0]      wdata_sel [BANKS];
    logic [BANKS-1:0] wsel_valid;

    integer bi, li;
    always_comb begin
        for (bi = 0; bi < BANKS; bi = bi + 1) begin
            min_addr[bi] = 32'hFFFF_FFFF;
            for (li = 0; li < LANES; li = li + 1) begin
                if (pend_q[li] && (addr_q[li][4:2] == 3'(bi)) && (addr_q[li] < min_addr[bi]))
                    min_addr[bi] = addr_q[li];
            end
        end
        for (li = 0; li < LANES; li = li + 1)
            serve[li] = pend_q[li] && (addr_q[li] == min_addr[addr_q[li][4:2]]);
    end

    integer ws, lw2;
    always_comb begin
        for (ws = 0; ws < BANKS; ws = ws + 1) begin
            wdata_sel[ws] = 32'd0;
            wsel_valid[ws] = 1'b0;
        end
        // 从高 lane 往低扫: 最后落地的是最低 lane(非阻塞赋值同拍覆盖)
        for (lw2 = LANES - 1; lw2 >= 0; lw2 = lw2 - 1) begin
            if (serve[lw2]) begin
                wdata_sel[addr_q[lw2][4:2]] = wdata_q[lw2];
                wsel_valid[addr_q[lw2][4:2]] = 1'b1;
            end
        end
    end

    // ---- 错误检查(组合, 与请求同拍; 只在 req_valid 有效时成立) ----
    integer ei;
    always_comb begin
        err_align = 1'b0;
        err_oob   = 1'b0;
        for (ei = 0; ei < LANES; ei = ei + 1) begin
            if (req_valid && lane_en[ei]) begin
                err_align |= (lane_addr[ei][1:0] != 2'b00);
                err_oob   |= (lane_addr[ei] >= 32'(SIZE_BYTES));
            end
        end
    end

    // ---- 状态机 ----
    always_ff @(posedge clk) begin
        if (rst) st <= S_IDLE;
        else     st <= st_n;
    end

    always_comb begin
        st_n = st;
        case (st)
            S_IDLE : if (req_valid && !err_align && !err_oob) st_n = S_ROUND;
            S_ROUND: st_n = (pend_q == '0) ? S_DONE : S_ROUND;
            S_DONE : st_n = S_IDLE;
            default: st_n = S_IDLE;
        endcase
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            we_q    <= 1'b0;
            pend_q  <= '0;
            addr_q  <= '0;
            wdata_q <= '0;
            round_q <= '0;
            rdata_r <= '0;
            for (bri = 0; bri < BANKS; bri = bri + 1)
                for (lri = 0; lri < WORDS_BANK; lri = lri + 1)
                    bank_mem[bri][lri] <= 32'd0;
        end else begin
            case (st)
                S_IDLE: begin
                    if (req_valid && !err_align && !err_oob) begin
                        we_q    <= req_we;
                        pend_q  <= lane_en;
                        addr_q  <= lane_addr;
                        wdata_q <= lane_wdata;
                        round_q <= 4'd0;
                        rdata_r <= '0;
                    end
                end
                S_ROUND: begin
                    if (pend_q != '0) begin
                        for (bw = 0; bw < BANKS; bw = bw + 1) begin
                            if (min_addr[bw] != 32'hFFFF_FFFF) begin
                                if (we_q) begin
                                    if (wsel_valid[bw])
                                        bank_mem[bw][min_addr[bw][5+:IDX_W]] <= wdata_sel[bw];
                                end else begin
                                    for (lw = 0; lw < LANES; lw = lw + 1)
                                        if (serve[lw] && (addr_q[lw][4:2] == 3'(bw)))
                                            rdata_r[lw] <= bank_mem[bw][addr_q[lw][5+:IDX_W]];
                                end
                            end
                        end
                        pend_q  <= pend_q & ~serve;
                        round_q <= round_q + 4'd1;
                    end
                end
                default: ;
            endcase
        end
    end

    assign lane_rdata     = rdata_r;
    assign req_ready      = (st == S_IDLE);
    assign rsp_valid      = (st == S_DONE);
    assign conflict_extra = (round_q == 4'd0) ? 4'd0 : (round_q - 4'd1);

endmodule

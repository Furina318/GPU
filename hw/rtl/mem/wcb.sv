`include "defines.vh"

// ============================================================================
//  wcb —— 写合并缓冲(MEM_LSU.md §5 D1/D2,ISA §2.4/§2.5)
//
//  每个 SM 一份,8 项 × 32B 行。全局 store 先进这里按行合并,满足以下条件才写出去
//  (`out_*` 端口交给 `global_mem` 发 AXI 写):
//    · 缓冲区满(第 9 项要进来)—— 由上层先 `flush_req`;
//    · `exit` / `bar.sync` 要求可见 —— 上层拉 `flush_req`。
//
//  **读旁路(D2)**:`ld.g` 先查这里 ——
//    · 整行命中(请求的字都被写过)⇒ `rd_hit`,直接返回,不碰 AXI;
//    · 部分命中 ⇒ 走 AXI 读,回来时用 `mg_*` 端口把本缓冲里更新过的字叠上去
//      (store 之后立刻 load 必须看到新值)。
//
//  `wen` 是 32b 字掩码(ISA §3.2:全部字访问,没有字节写)。
// ============================================================================

module wcb #(
    parameter int ENTRIES = 8,
    parameter int WORDS   = 8          // 32B 行 = 8 个 32b 字
) (
    input  wire                    clk         ,
    input  wire                    rst         ,
    // ---- 写合并入口(store) ----
    input  wire                    wr_valid    ,
    input  wire [26:0]             wr_line     ,   // addr[31:5]
    input  wire [WORDS-1:0]        wr_wen      ,
    input  wire [WORDS-1:0][31:0]  wr_wdata    ,
    output logic                   wr_ready    ,   // 有空位,或命中已有项
    // ---- 读旁路(load) ----
    input  wire [26:0]             rd_line     ,
    input  wire [WORDS-1:0]        rd_wen      ,   // 想读的字(load 通常单字)
    output logic                   rd_hit      ,   // 整行命中:不必访问内存
    output logic [WORDS-1:0][31:0] rd_data     ,
    // ---- 读合并(AXI 回来的数据 + 本缓冲更新的字) ----
    input  wire [26:0]             mg_line     ,
    input  wire [WORDS-1:0][31:0]  mg_data_in  ,
    output logic [WORDS-1:0][31:0] mg_data_out ,
    // ---- 冲刷 ----
    input  wire                    flush_req   ,   // 电平: 冲刷到空为止
    output logic                   flush_done  ,   // 脉冲: 本次冲刷完成
    output logic                   empty       ,
    output logic                   full        ,   // 缓冲已满(需要腾位置)
    // ---- 写出口(每次一项,交给 global_mem) ----
    output logic                   out_valid   ,
    output logic [26:0]            out_line    ,
    output logic [WORDS-1:0]       out_wen     ,
    output logic [WORDS-1:0][31:0] out_wdata   ,
    input  wire                    out_ready
);

    localparam int IDX_W = (ENTRIES > 1) ? $clog2(ENTRIES) : 1;

    logic                    vld      [ENTRIES];
    logic [26:0]             line_mem [ENTRIES];
    logic [WORDS-1:0]        ok_mem   [ENTRIES];      // 本缓冲写过的字
    logic [WORDS-1:0][31:0]  dat_mem  [ENTRIES];

    logic [IDX_W:0]          cnt;                     // 有效项数

    // ---- 组合查询:写命中项 / 空项 ----
    logic [IDX_W-1:0] hit_idx, free_idx;
    logic             hit_found, free_found;

    always_comb begin : find_slots
        integer qi;
        hit_idx    = '0;
        hit_found  = 1'b0;
        free_idx   = '0;
        free_found = 1'b0;
        for (qi = 0; qi < ENTRIES; qi = qi + 1) begin
            if (vld[qi] && (line_mem[qi] == wr_line)) begin
                hit_found = 1'b1;
                hit_idx   = IDX_W'(qi);
            end
            if (!vld[qi] && !free_found) begin
                free_found = 1'b1;
                free_idx   = IDX_W'(qi);
            end
        end
    end

    assign wr_ready = hit_found || free_found;

    // ---- 读旁路 ----
    always_comb begin : read_bypass
        integer ri;
        rd_hit  = 1'b0;
        rd_data = '0;
        for (ri = 0; ri < ENTRIES; ri = ri + 1) begin
            if (vld[ri] && (line_mem[ri] == rd_line) &&
                ((rd_wen & ~ok_mem[ri]) == '0)) begin
                rd_hit  = 1'b1;
                rd_data = dat_mem[ri];
            end
        end
    end

    // ---- 读合并:内存回来的行 + 本缓冲更新的字 ----
    always_comb begin : read_merge
        integer mi;
        mg_data_out = mg_data_in;
        for (mi = 0; mi < ENTRIES; mi = mi + 1) begin
            if (vld[mi] && (line_mem[mi] == mg_line)) begin
                for (int wi = 0; wi < WORDS; wi = wi + 1)
                    if (ok_mem[mi][wi])
                        mg_data_out[wi] = dat_mem[mi][wi];
            end
        end
    end

    // ---- 写出口:选一个待冲刷项(最小编号优先) ----
    logic [IDX_W-1:0] out_idx;
    always_comb begin : pick_out
        integer oi;
        logic   found;                  // 局部变量: 不能拿 out_valid 自己当循环条件
        found     = 1'b0;
        out_idx   = '0;
        out_valid = 1'b0;
        out_line  = '0;
        out_wen   = '0;
        out_wdata = '0;
        for (oi = 0; oi < ENTRIES; oi = oi + 1) begin
            if (vld[oi] && !found) begin
                found     = 1'b1;
                out_valid = 1'b1;
                out_idx   = IDX_W'(oi);
                out_line  = line_mem[oi];
                out_wen   = ok_mem[oi];
                out_wdata = dat_mem[oi];
            end
        end
    end

    // ---- 状态 ----
    logic do_alloc, do_remove;

    assign do_alloc  = wr_valid && wr_ready && !hit_found;
    assign do_remove = out_valid && out_ready;
    assign empty     = (cnt == '0);
    assign full      = !free_found;

    always_ff @(posedge clk) begin
        if (rst) begin
            for (int i = 0; i < ENTRIES; i = i + 1) begin
                vld[i]      <= 1'b0;
                line_mem[i] <= '0;
                ok_mem[i]   <= '0;
                dat_mem[i]  <= '0;
            end
            cnt        <= '0;
            flush_done <= 1'b0;
        end else begin
            flush_done <= 1'b0;

            // 写合并(命中 ⇒ 就地更新;未命中 ⇒ 占一个空项)
            if (wr_valid && wr_ready) begin
                if (hit_found) begin
                    for (int wi = 0; wi < WORDS; wi = wi + 1) begin
                        if (wr_wen[wi]) begin
                            dat_mem[hit_idx][wi] <= wr_wdata[wi];
                            ok_mem[hit_idx][wi]  <= 1'b1;
                        end
                    end
                end else begin
                    vld[free_idx]      <= 1'b1;
                    line_mem[free_idx] <= wr_line;
                    for (int wi = 0; wi < WORDS; wi = wi + 1) begin
                        dat_mem[free_idx][wi] <= wr_wdata[wi];
                        ok_mem[free_idx][wi]  <= wr_wen[wi];
                    end
                end
            end

            // 冲刷:每次交出一项
            if (do_remove)
                vld[out_idx] <= 1'b0;

            // 计数
            cnt <= cnt + (do_alloc ? {{IDX_W{1'b0}}, 1'b1} : '0) -
                         (do_remove ? {{IDX_W{1'b0}}, 1'b1} : '0);

            // 冲刷完成:本拍之后缓冲为空且上层仍要求冲刷
            if (flush_req &&
                (cnt + (do_alloc ? {{IDX_W{1'b0}}, 1'b1} : '0) -
                       (do_remove ? {{IDX_W{1'b0}}, 1'b1} : '0)) == '0)
                flush_done <= 1'b1;
        end
    end

endmodule

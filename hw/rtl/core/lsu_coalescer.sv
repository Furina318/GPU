`include "defines.vh"

// ============================================================================
//  lsu_coalescer —— 8 个 lane 的地址 → N 个 32B 行请求(纯组合, ISA §3.2)
//
//  合并规则(ISA §3.2 的四种情形):
//    ① 8 lane 连续 / 同一 32B 行 ⇒ 1 个请求;
//    ② 全部 lane 同地址          ⇒ 1 个请求, 回填时广播;
//    ③ 部分 lane 被谓词屏蔽      ⇒ 只对剩余 lane 的连续段出请求;
//    ④ 地址分散                  ⇒ 最多 LANES 个请求.
//
//  输出按 `addr[31:5]` **升序**;`req_lanes` 是该行覆盖的 lane 集合,
//  `req_wen` 是该行内要写的 32b 字(bit i = 行内第 i 个字, 只有 store 才有意义),
//  `lane_word` 是每个 lane 的行内字号(load 回填按它拆回 lane)。
//
//  写数据不在这里拼(D5): store 的 8 个字由 `global_mem`/WCB 从 `lane_wdata`
//  收集, 这里只出"写哪几行、每行写哪几个字", 省掉约 2 kb 的 mux。
// ============================================================================

module lsu_coalescer #(
    parameter int LANES = `WARP_SIZE
) (
    input  wire [LANES-1:0]             lane_en   ,   // effective_mask(active ∩ 谓词)
    input  wire [LANES-1:0][31:0]       lane_addr ,
    input  wire                         is_store  ,
    output logic [3:0]                  req_num   ,   // 0..LANES
    output logic [LANES-1:0][31:0]      req_line  ,   // 32B 对齐的行地址(升序)
    output logic [LANES-1:0][LANES-1:0] req_lanes ,   // 该行覆盖的 lane 集合
    output logic [LANES-1:0][7:0]       req_wen   ,   // 行内要写的字(bit = addr[4:2])
    output logic [LANES-1:0][2:0]       lane_word     // lane → 行内字号
);

    integer li, rj;

    logic [31:0]      min_line;
    logic             found;
    logic [LANES-1:0] used;

    always_comb begin
        used      = '0;
        req_num   = 4'd0;
        req_line  = '0;
        req_lanes = '0;
        req_wen   = '0;
        lane_word = '0;
        min_line  = 32'hFFFF_FFFF;
        found     = 1'b0;

        for (li = 0; li < LANES; li = li + 1)
            lane_word[li] = lane_addr[li][4:2];

        for (rj = 0; rj < LANES; rj = rj + 1) begin
            min_line = 32'hFFFF_FFFF;
            found    = 1'b0;
            // ① 在未服务的 lane 里取最小的行号 addr[31:5]
            for (li = 0; li < LANES; li = li + 1) begin
                if (lane_en[li] && !used[li] && (lane_addr[li][31:5] < min_line[31:5])) begin
                    min_line = {lane_addr[li][31:5], 5'b0};
                    found    = 1'b1;
                end
            end
            // ② 同行 lane 归成一组, 行内每个字按 addr[4:2] 置 wen
            if (found) begin
                req_line[rj] = min_line;
                for (li = 0; li < LANES; li = li + 1) begin
                    if (lane_en[li] && !used[li] && (lane_addr[li][31:5] == min_line[31:5])) begin
                        req_lanes[rj][li]               = 1'b1;
                        req_wen[rj][lane_addr[li][4:2]] = is_store;
                        used[li]                        = 1'b1;
                    end
                end
                req_num = req_num + 4'd1;
            end
        end
    end

endmodule

// ============================================================================
// cmd_cache.sv —— 命令环的行缓存
//
// CP 取命令的粒度是 64B 行(一条命令最多跨一行)。这个缓存就是"命令环的窗口":
//   * 直接映射:CACHE_SIZE 组,每组存一行 64B(16 个 32b 字,一个字一个 bank)
//   * 命中 2 拍:接受请求 1 拍 + 标签/数据同拍读出判命中 1 拍
//   * 缺失:发一次内存读,回来的数据按 AXI R 的节奏一拍一个字写进对应 bank,
//           最后一拍同拍发 SRAM 读,下一拍就能把整行交给 CP
//
// 失效按**范围**而不是全片:host 的发布协议是"先写命令字节,再写 TAIL/DOORBELL",
// 所以只有 [上次发布的 tail, 这次发布的 tail) 这一段可能变;把它按行失效即可。
// 这样跨批次的热行不会被门铃打冷(全片失效只留给 CP_RESET 与搬动整个读区域
// 的 CMD_HEAD / CMD_QUEUE_BASE / CMD_QUEUE_SIZE)。
//
// 预取由"发布事件"踢一脚:发布一旦发生,缓存就在后台从新区域第一行一路走到
// 新 tail(期间 CP 可能在等 SM/引擎)。走到头就停;走到环尾的下一条会折返到环首。
// CP 没发布新命令时缓存不会自己去动内存 —— 队列空时范围为空,自然不动。
// ============================================================================
module cmd_cache #(
    parameter int ADDR_W     = 32,
    parameter int CACHE_SIZE = 64,           // 组数(默认覆盖整个 4KB 命令环)
    parameter int BLOCK_SIZE = 16,           // 行内字数(16 字 = 64B)
    parameter int OFF_W      = 6             // 行内地址位数(64B 行)
) (
    input  wire                      clk            ,
    input  wire                      rst            ,

    // ── CP 侧:取一整行 ──────────────────────────────────────────────────────
    input  wire                      req_valid      ,   // 电平:保持到看见 rsp_valid
    input  wire [ADDR_W-OFF_W-1:0]   req_line       ,   // 行号(地址 >> 6)
    output logic                     rsp_valid      ,   // 单拍脉冲
    output logic [ADDR_W-OFF_W-1:0]  rsp_line       ,   // 对应的行号
    output logic [BLOCK_SIZE*32-1:0] rsp_data       ,
    output logic                     rsp_err        ,

    // ── 发布 / 失效事件 ────────────────────────────────────────────────────
    // 命令环里 [pub_start_line, pub_end_line) 是(重新)发布的新区域:
    // 这些行失效,同时成为预取的目标。pub_valid 保证这个范围非空。
    input  wire                      pub_valid      ,
    input  wire [ADDR_W-OFF_W-1:0]   pub_start_line ,
    input  wire [ADDR_W-OFF_W-1:0]   pub_end_line   ,
    // 全片失效(CP_RESET / 搬动整个读区域)。与 pub_valid 同拍时:先全清再按范围预取。
    input  wire                      inval_all      ,
    // 命令环的行号范围 [ring_base_line, ring_end_line),用于预取折返与边界判断
    input  wire [ADDR_W-OFF_W-1:0]   ring_base_line ,
    input  wire [ADDR_W-OFF_W-1:0]   ring_end_line  ,

    // CP 自己的单字读/写正在用内存口:缓存先别开始回填
    input  wire                      port_free      ,

    // ── 内存侧(AXI R 形状的拍流)────────────────────────────────────────────
    output logic                     m_rd_req       ,
    output logic [ADDR_W-1:0]        m_rd_addr      ,
    input  wire                      m_rd_ack       ,   // 请求已被收下
    input  wire                      m_rd_vld       ,   // 本拍有一个字
    input  wire [31:0]               m_rd_word      ,
    input  wire                      m_rd_last      ,   // 本行最后一个字
    input  wire                      m_rd_err       ,
    output logic                     busy           ,

    // ── 统计(只读,给测试与调试用)──────────────────────────────────────────
    output logic [31:0]              ctr_hit        ,   // CP 的真实请求命中
    output logic [31:0]              ctr_miss       ,   // CP 的真实请求缺失
    output logic [31:0]              ctr_pref       ,   // 预取装进来的行数
    output logic [31:0]              ctr_pub        ,   // 发布事件次数
    output logic [31:0]              ctr_inval      ,   // 全片失效次数
    output logic [31:0]              ctr_rdline          // 发出去的行读次数
);

    localparam int LINE_W = ADDR_W - OFF_W;                              // 行号位宽
    localparam int IDX_W  = (CACHE_SIZE > 1) ? $clog2(CACHE_SIZE) : 1;   // 组号位宽
    localparam int TAG_W  = ADDR_W - IDX_W - OFF_W;                      // 标签位宽
    localparam int BEAT_W = (BLOCK_SIZE > 1) ? $clog2(BLOCK_SIZE) : 1;

    typedef enum logic [1:0] {
        C_IDLE = 2'd0,
        C_CHK  = 2'd1,      // 标签/数据同拍读出判命中
        C_MISS = 2'd2,      // 未命中:发请求 + 按拍收数据(每拍写一个 bank)
        C_RSP  = 2'd3       // 回给 CP。最后一拍收字那一拍同时就发 SRAM 读,
                            // 所以这里 bank_pack 已经是新数据,不需要单独的读回状态
    } cc_state_t;

    cc_state_t          state;
    logic [LINE_W-1:0]  a_line_q;     // 本次请求的行号
    logic [IDX_W-1:0]   idx_q;
    logic [TAG_W-1:0]   tag_q;
    logic               is_pf;        // 本次只装缓存,不回 rsp
    logic               rearm;        // 在飞的请求被失效打断 ⇒ 装完重来
    logic               fill_acked;
    logic               req_seen;     // 已经接过这一次请求(req_valid 是电平)
    logic [BEAT_W-1:0]  beat_q;
    logic               err_q;
    logic [31:0]        last_word_q;  // 回填最后一拍的字(那个 bank 当拍在读不到)

    // 每行一个有效位(不用 epoch):范围失效时只清落在范围内的组。有效位是触发器,
    // 与标签 SRAM 的读同拍可用,命中路径不多一拍。
    logic [CACHE_SIZE-1:0] valid_q;

    // 预取窗口:发布事件给出"从哪一行起、还有几行"。游标走到环尾的下一条折返到
    // 环首(命令环回绕),走完 pf_left_q 行就停。没有发布过就一行都不预取。
    logic               pf_pend;
    logic [LINE_W:0]    pf_left_q;    // 还要预取几行(0 = 窗口空了)
    logic [LINE_W-1:0]  pf_line_q;

    // ── 失效范围 → 组掩码 ───────────────────────────────────────────────────
    wire [LINE_W:0] ring_lines = {1'b0, ring_end_line} - {1'b0, ring_base_line};

    // 发布范围的长度(行数):正常是 end - start;越过环尾折回环首时加一圈
    wire [LINE_W:0] pub_len = (pub_end_line >= pub_start_line)
                            ? ({1'b0, pub_end_line} - {1'b0, pub_start_line})
                            : ({1'b0, pub_end_line} + ring_lines - {1'b0, pub_start_line});

    // 从组 s 起连续 len 行覆盖到的组(按组号取模)。len >= CACHE_SIZE 就是全清。
    function automatic logic [CACHE_SIZE-1:0] span_mask(input logic [IDX_W-1:0] s,
                                                        input logic [LINE_W:0] len);
        integer i;
        logic [LINE_W:0] rel;
        begin
            span_mask = '0;
            if (len >= (LINE_W+1)'(CACHE_SIZE)) begin
                span_mask = '1;
            end
            else begin
                for (i = 0; i < CACHE_SIZE; i = i + 1) begin
                    rel = (LINE_W+1)'((i + CACHE_SIZE - int'(s)) % CACHE_SIZE);
                    span_mask[i] = (rel < len);
                end
            end
        end
    endfunction

    wire [CACHE_SIZE-1:0] pub_mask = pub_valid ? span_mask(pub_start_line[IDX_W-1:0], pub_len) : '0;
    wire [CACHE_SIZE-1:0] clr_mask = inval_all ? '1 : pub_mask;

    // ── 本拍要访问哪一行 ───────────────────────────────────────────────────
    logic [LINE_W-1:0] cur_line;
    logic [IDX_W-1:0]  cur_idx;
    logic [TAG_W-1:0]  cur_tag;
    logic              hit;
    logic [IDX_W-1:0]  tagv_addra;
    logic [TAG_W-1:0]  tagv_dina;
    logic [TAG_W-1:0]  tagv_douta;
    logic              tagv_ena;
    logic              tagv_wea;

    logic [IDX_W-1:0]  bank_addra [BLOCK_SIZE];
    logic [31:0]       bank_dina  [BLOCK_SIZE];
    logic [31:0]       bank_douta [BLOCK_SIZE];
    logic              bank_ena   [BLOCK_SIZE];
    logic [3:0]        bank_wea   [BLOCK_SIZE];
    logic [BLOCK_SIZE*32-1:0] bank_pack;

    logic fill_done;          // 本拍回填收尾(数据齐、无错、没被打断)

    always_comb begin
        for (int q = 0; q < BLOCK_SIZE; q = q + 1)
            bank_pack[q*32 +: 32] = bank_douta[q];
    end

    always_comb begin
        // 这一拍要接受哪一行,就必须读哪一行的标签 —— 否则会拿 A 的标签判 B 的命中
        // (命令环里相邻两行的 tag 往往相同,只差组号,判错就是假命中)
        if (state != C_IDLE)             cur_line = a_line_q;
        else if (req_valid && !req_seen) cur_line = req_line;
        else if (pf_pend)                cur_line = pf_line_q;
        else                             cur_line = a_line_q;
        cur_idx = cur_line[IDX_W-1:0];
        cur_tag = cur_line[LINE_W-1:IDX_W];

        // 本拍发 SRAM 读的时机:
        //   1) 接受一个新请求/预取
        //   2) 回填的最后一拍(15 个 bank 读出刚写进去的新数据,正在写的那个由
        //      last_word_q 补)
        //   3) 失效后重来
        tagv_addra = cur_idx;
        for (int b = 0; b < BLOCK_SIZE; b = b + 1) begin
            bank_addra[b] = cur_idx;
            bank_dina[b]  = m_rd_word;
            bank_ena[b]   = ((state == C_IDLE) & ((req_valid && !req_seen) | pf_pend | rearm))
                          | ((state == C_MISS) & m_rd_vld & m_rd_last);
            bank_wea[b]   = 4'h0;
        end
        tagv_dina = tag_q;
        tagv_ena  = bank_ena[0];
        tagv_wea  = 1'b0;

        // 回填:本拍这一个字写进它所在字位的 bank
        if ((state == C_MISS) && m_rd_vld) begin
            bank_addra[beat_q] = idx_q;
            bank_dina[beat_q]  = m_rd_word;
            bank_ena[beat_q]   = 1'b1;
            bank_wea[beat_q]   = 4'hF;
        end

        // 回填收尾:把标签写进这一组(出错的行不写,否则总线错误会变成数据错误)
        if ((state == C_MISS) && m_rd_vld && m_rd_last) begin
            tagv_addra = idx_q;
            tagv_dina  = tag_q;
            tagv_ena   = 1'b1;
            tagv_wea   = !rearm && !m_rd_err;
        end
    end

    // 命中:有效位(当拍可用)& 标签相同。同一拍被失效覆盖的组,宁可缺失重读。
    assign hit = valid_q[cur_idx] & (tagv_douta == cur_tag) & ~clr_mask[cur_idx];

    // 回填收尾:最后一个字也到了,没出错、没被打断
    assign fill_done = (state == C_MISS) && m_rd_vld && m_rd_last && !m_rd_err && !rearm;

    // ── 预取游标 ───────────────────────────────────────────────────────────
    wire [LINE_W-1:0] adv_line   = (a_line_q + 1 == ring_end_line) ? ring_base_line
                                                                  : a_line_q + 1;
    wire              pf_in_ring = (pf_line_q >= ring_base_line) && (pf_line_q < ring_end_line);
    wire              dem_in_ring= (a_line_q  >= ring_base_line) && (a_line_q  < ring_end_line);

    // 真实请求拿到行之后,若预取窗口里还有行没走,就把"下一行"挂上
    wire pf_kick_dem = !is_pf && dem_in_ring && !pf_pend && (pf_left_q != '0) && !pub_valid;

    always_ff @(posedge clk) begin
        if (rst) begin
            valid_q    <= '0;                 // 有效位是触发器,复位清空 ⇒ 不会命中复位前的行
            last_word_q<= 32'h0;
        end
        else begin
            valid_q <= valid_q & ~clr_mask;
            if (fill_done) valid_q[idx_q] <= ~clr_mask[idx_q];
            if ((state == C_MISS) && m_rd_vld && m_rd_last) last_word_q <= m_rd_word;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state      <= C_IDLE;
            a_line_q   <= '0;
            idx_q      <= '0;
            tag_q      <= '0;
            is_pf      <= 1'b0;
            rearm      <= 1'b0;
            fill_acked <= 1'b0;
            req_seen   <= 1'b0;
            beat_q     <= '0;
            err_q      <= 1'b0;
            pf_pend    <= 1'b0;
            pf_left_q  <= '0;
            pf_line_q  <= '0;
            rsp_valid  <= 1'b0;
            rsp_line   <= '0;
            rsp_data   <= '0;
            rsp_err    <= 1'b0;
            ctr_hit    <= '0;
            ctr_miss   <= '0;
            ctr_pref   <= '0;
            ctr_pub    <= '0;
            ctr_inval  <= '0;
            ctr_rdline <= '0;
        end
        else begin
            rsp_valid <= 1'b0;
            rsp_err   <= 1'b0;

            // req_valid 是电平:接过一次就置位,等 CP 撤掉才放开
            // (否则 rsp 那一拍 CP 还举着 req,下一拍会被重复接受,吐出多余的 rsp)
            if (!req_valid) req_seen <= 1'b0;

            // ── 失效与发布 ──────────────────────────────────────────────────
            if (inval_all) begin
                pf_pend   <= 1'b0;
                pf_left_q <= '0;
                ctr_inval <= ctr_inval + 32'd1;
                if (state != C_IDLE) rearm <= 1'b1;
            end
            if (pub_valid) begin
                // 新区域:作废范围内的行(有效位在另一个块里清),并把游标挪到起点
                ctr_pub   <= ctr_pub + 32'd1;
                pf_pend   <= (pub_len != '0);
                pf_left_q <= (pub_len >= (LINE_W+1)'(CACHE_SIZE))
                           ? (LINE_W+1)'(CACHE_SIZE) : pub_len;
                pf_line_q <= pub_start_line;
                if (state != C_IDLE) begin
                    // 在飞的行落在新区域里 ⇒ 装完也是旧数据,重来
                    if (clr_mask[a_line_q[IDX_W-1:0]]) rearm <= 1'b1;
                end
            end

            case (state)
            C_IDLE: begin
                // 优先级:真实请求 > 失效后重来 > 预取
                if (req_valid && !req_seen) begin
                    a_line_q <= req_line; idx_q <= cur_idx; tag_q <= cur_tag;
                    beat_q   <= '0;
                    is_pf <= 1'b0; fill_acked <= 1'b0; req_seen <= 1'b1;
                    state <= C_CHK;
                end
                else if (rearm) begin
                    rearm <= 1'b0; fill_acked <= 1'b0;
                    state <= C_CHK;                    // 同一个请求重来
                end
                else if (pf_pend) begin
                    pf_pend <= 1'b0;
                    // 游标掉到环外(驱动改了基址/大小)就别再预取了
                    if (pf_in_ring) begin
                        a_line_q <= pf_line_q; idx_q <= cur_idx; tag_q <= cur_tag;
                        beat_q   <= '0;
                        is_pf <= 1'b1; fill_acked <= 1'b0;
                        state <= C_CHK;
                    end
                end
            end

            C_CHK: begin
                if (hit) begin
                    if (!is_pf) begin
                        rsp_valid <= 1'b1;
                        rsp_line  <= a_line_q;
                        rsp_data  <= bank_pack;
                        ctr_hit   <= ctr_hit + 32'd1;
                        if (pf_kick_dem) begin
                            pf_pend   <= 1'b1;
                            pf_line_q <= adv_line;
                        end
                    end
                    else begin
                        // 预取命中:游标接着往后走
                        ctr_pref  <= ctr_pref + 32'd1;
                        pf_line_q <= adv_line;
                        pf_left_q <= pf_left_q - {{LINE_W{1'b0}}, 1'b1};
                        pf_pend   <= (pf_left_q > {{LINE_W{1'b0}}, 1'b1});
                    end
                    state <= C_IDLE;
                end
                else if (port_free) begin
                    if (!is_pf) ctr_miss <= ctr_miss + 32'd1;
                    ctr_rdline <= ctr_rdline + 32'd1;   // 发一次行读
                    state <= C_MISS;
                end
                // 端口被 CP 的单字读写占着:留在本态等,输出保持不变
            end

            // 一个状态里同时等 ack 和数据:数据按拍进(每拍写一个 bank),
            // 收下请求(ack)就把请求线撤掉。两者互相独立,不需要两个状态。
            C_MISS: begin
                if (m_rd_vld) begin
                    beat_q <= beat_q + {{(BEAT_W-1){1'b0}}, 1'b1};
                    if (m_rd_last) begin
                        err_q <= m_rd_err;
                        state <= C_RSP;      // 这一拍已发了 SRAM 读,下一拍就能回
                    end
                end
                else if (m_rd_ack) begin
                    fill_acked <= 1'b1;
                end
            end

            C_RSP: begin
                if (!rearm && !is_pf) begin
                    rsp_valid <= 1'b1;
                    rsp_line  <= a_line_q;
                    // 行内最后一个字的 bank 这一拍正在写,读不回来 ⇒ 用 last_word_q 顶上去
                    rsp_data  <= {last_word_q, bank_pack[BLOCK_SIZE*32-33:0]};
                    rsp_err   <= err_q;
                    if (pf_kick_dem) begin
                        pf_pend   <= 1'b1;
                        pf_line_q <= adv_line;
                    end
                end
                else if (is_pf) begin
                    if (!err_q) ctr_pref <= ctr_pref + 32'd1;
                    pf_line_q <= adv_line;
                    pf_left_q <= rearm ? pf_left_q
                                       : pf_left_q - {{LINE_W{1'b0}}, 1'b1};
                    pf_pend   <= !rearm && (pf_left_q > {{LINE_W{1'b0}}, 1'b1});
                end
                state <= C_IDLE;
            end

            default: state <= C_IDLE;
            endcase

        end
    end

    assign m_rd_req  = (state == C_MISS) && !fill_acked;
    assign m_rd_addr = {a_line_q, {OFF_W{1'b0}}};
    // busy 只表示"缓存真的占着内存口":C_CHK 在等端口时不算,否则会和 CP 互锁
    assign busy      = (state == C_MISS) || (state == C_RSP);

    tagv_sram #(
        .WIDTH (TAG_W    ),
        .DEPTH (CACHE_SIZE)
    ) u_tagv (
        .addra (tagv_addra),
        .clka  (clk       ),
        .dina  (tagv_dina ),
        .douta (tagv_douta),
        .ena   (tagv_ena  ),
        .wea   (tagv_wea  )
    );

    genvar gb;
    generate
        for (gb = 0; gb < BLOCK_SIZE; gb = gb + 1) begin : g_bank
            data_bank_sram #(
                .WIDTH (32        ),
                .DEPTH (CACHE_SIZE)
            ) u_bank (
                .addra (bank_addra[gb]),
                .clka  (clk           ),
                .dina  (bank_dina[gb] ),
                .douta (bank_douta[gb]),
                .ena   (bank_ena[gb]  ),
                .wea   (bank_wea[gb]  )
            );
        end
    endgenerate

endmodule

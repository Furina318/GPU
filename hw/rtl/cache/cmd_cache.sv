module cmd_cache #(
    parameter int ADDR_W     = 32,
    parameter int CACHE_SIZE = 64,           // 组数(默认覆盖整个 4KB 命令环)
    parameter int BLOCK_SIZE = 16,           // 行内字数(16 字 = 64B)
    parameter int OFF_W      = 6             // 行内地址位数(64B 行)
) (
    input  wire                     clk            ,
    input  wire                     rst            ,

    // CP 侧:取一整行
    input  wire                     req_valid      ,
    input  wire [ADDR_W-OFF_W-1:0]  req_line       ,   // 行号(地址 >> 6)
    output logic                    rsp_valid      ,   // 单拍脉冲
    output logic [ADDR_W-OFF_W-1:0] rsp_line       ,   // 命中的行号
    output logic [BLOCK_SIZE*32-1:0] rsp_data      ,
    output logic                    rsp_err        ,

    // 顺序预取的上界
    input  wire [ADDR_W-OFF_W-1:0]  ring_end_line  ,

    // 失效:host 写了 DOORBELL 或 CP_RESET
    input  wire                     invalidate     ,

    // CP 自己的单字读/写正在用内存口, 缓存先别开始回填
    input  wire                     port_free      ,

    // 内存侧
    output logic                    m_rd_req       ,
    output logic [ADDR_W-1:0]       m_rd_addr      ,
    input  wire                     m_rd_ack       , // 内存测收到请求
    input  wire                     m_rd_vld       , // 本拍有一个字
    input  wire [31:0]              m_rd_word      ,
    input  wire                     m_rd_last      , // 本行的最后一个字
    input  wire                     m_rd_err       ,
    output logic                    busy           
);

    localparam int IDX_W   = (CACHE_SIZE > 1) ? $clog2(CACHE_SIZE) : 1;
    localparam int TAG_W   = ADDR_W - IDX_W - OFF_W;
    localparam int EPOCH_W = 16;
    localparam int TAGV_W  = TAG_W + EPOCH_W + 1;                  // {epoch, tag, valid}

    typedef enum logic [1:0] {
        C_IDLE = 2'd0,
        C_CHK  = 2'd1,      
        C_MISS = 2'd2,      
        C_RSP  = 2'd3       
                            
    } cc_state_t;

    // 顺序预取: 每次**真实请求**拿到行 L 之后, 把 L+1 挂上待预取
    wire [ADDR_W-OFF_W-1:0] a_line_next = a_line_q + {{(ADDR_W-OFF_W-1){1'b0}}, 1'b1};
    wire                    pf_ok       = (a_line_next < ring_end_line);

    cc_state_t         state;
    logic [ADDR_W-OFF_W-1:0] a_line_q;  // 本次请求的行号(地址去掉低 6 位)
    logic [IDX_W-1:0]  idx_q;
    logic [TAG_W-1:0]  tag_q;
    logic              is_pf;           // 本次只装缓存,不回 rsp
    logic              rearm;           // 失效打断了在飞的请求 ⇒ 装完重来
    logic              fill_acked;
    logic              req_seen;        // 已经接过这一次请求(req_valid 是电平)
    logic [BEAT_W-1:0] beat_q;         // 回填到行内第几个字
    logic              err_q;          // 本次回填是否出过错(最后一个字那一拍记下)
    logic              pf_pend;         // 有一行等着预取(非阻塞: 真实请求优先)
    logic [ADDR_W-OFF_W-1:0] pf_line_q; // 要预取的行号 = 上一次访问的行 + 1
    logic [EPOCH_W-1:0] epoch;

    logic [ADDR_W-OFF_W-1:0] cur_line;   // 本拍要访问的行号(地址去掉低 6 位)
    logic [IDX_W-1:0]  cur_idx;
    logic [TAG_W-1:0]  cur_tag;
    logic              sr_ena;

    always_comb begin
        // 这一拍要接受哪一行, 就必须读哪一行的标签 —— 否则会拿 A 的标签判 B 的命中
        // (命令环里相邻两行的 tag 往往相同, 只差组号, 判错就是假命中)
        if (state != C_IDLE)             cur_line = a_line_q;
        else if (req_valid && !req_seen) cur_line = req_line;
        else if (pf_pend)                cur_line = pf_line_q;
        else                 cur_line = a_line_q;             // rearm:同一个请求重来
        cur_idx = cur_line[IDX_W-1:0];
        cur_tag = cur_line[ADDR_W-OFF_W-1:IDX_W];
        // 最后一拍收字的那一拍顺带发读: 15 个 bank 读出的是刚写进去的新数据,
        // 正在写的那个 bank(bank 15)读不回来 —— 由 last_word_q 补上。
        sr_ena  = ((state == C_IDLE) & ((req_valid && !req_seen) | pf_pend | rearm))
                | ((state == C_MISS) & m_rd_vld & m_rd_last);
    end

    localparam int BEAT_W = (BLOCK_SIZE > 1) ? $clog2(BLOCK_SIZE) : 1;

    logic [IDX_W-1:0]  tagv_addra;
    logic [TAGV_W-1:0] tagv_dina;
    logic [TAGV_W-1:0] tagv_douta;
    logic              tagv_ena;
    logic              tagv_wea;

    logic [IDX_W-1:0]  bank_addra [BLOCK_SIZE];
    logic [31:0]       bank_dina  [BLOCK_SIZE];
    logic [31:0]       bank_douta [BLOCK_SIZE];
    logic              bank_ena   [BLOCK_SIZE];
    logic [3:0]        bank_wea   [BLOCK_SIZE];

    logic [BLOCK_SIZE*32-1:0] bank_pack;

    always_comb begin
        for (int q = 0; q < BLOCK_SIZE; q = q + 1)
            bank_pack[q*32 +: 32] = bank_douta[q];
    end

    // 回填最后一拍的字: 那一拍它所在的 bank 正在写, SRAM 读不到, 单独记下来
    logic [31:0] last_word_q;
    always_ff @(posedge clk) begin
        if (rst) last_word_q <= 32'h0;
        else if ((state == C_MISS) && m_rd_vld && m_rd_last) last_word_q <= m_rd_word;
    end

    logic hit;
    assign hit = tagv_douta[0]
               & (tagv_douta[TAG_W:1] == tag_q)
               & (tagv_douta[TAGV_W-1:TAG_W+1] == epoch)
               & ~invalidate;         // 同一拍失效:宁可缺失重读,也不回旧数据

    always_comb begin
        tagv_addra = cur_idx;
        tagv_dina  = {epoch, tag_q, 1'b1};
        tagv_ena   = sr_ena;
        tagv_wea   = 1'b0;

        for (int b = 0; b < BLOCK_SIZE; b = b + 1) begin
            bank_addra[b] = cur_idx;
            bank_dina[b]  = m_rd_word;      // 回填时只有命中的那个 bank 会真正写
            bank_ena[b]   = sr_ena;
            bank_wea[b]   = 4'h0;
        end

        if ((state == C_MISS) && m_rd_vld) begin
            bank_addra[beat_q] = idx_q;
            bank_dina[beat_q]  = m_rd_word;
            bank_ena[beat_q]   = 1'b1;
            bank_wea[beat_q]   = 4'hF;
            if (m_rd_last) begin
                // 数据齐了才让这一行有效(出错的行不装, 否则总线错误会变成数据错误)
                tagv_addra = idx_q;
                tagv_dina  = {epoch, tag_q, 1'b1};
                tagv_ena   = 1'b1;
                tagv_wea   = !rearm && !m_rd_err;
            end
        end

        m_rd_req  = (state == C_MISS) && !fill_acked;
        m_rd_addr = {a_line_q, {OFF_W{1'b0}}};
        // busy 只表示"缓存真的占着内存口":C_CHK 在等端口时不算,否则会和 CP 互锁
        busy      = (state == C_MISS) || (state == C_RSP);
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
            pf_line_q  <= '0;
            // 复位也要让 epoch 前进:SRAM 里还留着复位前的 {tag, valid},
            // 若把 epoch 清回 0,复位后第一请求就可能命中复位前的旧行。
            epoch      <= epoch + {{(EPOCH_W-1){1'b0}}, 1'b1};
            rsp_valid  <= 1'b0;
            rsp_line   <= '0;
            rsp_data   <= '0;
            rsp_err    <= 1'b0;
        end
        else begin
            rsp_valid <= 1'b0;
            rsp_err   <= 1'b0;

            // req_valid 是电平:接过一次就置位,等 CP 撤掉才放开
            // (否则 rsp 那一拍 CP 还举着 req,下一拍会被重复接受,吐出多余的 rsp)
            if (!req_valid) req_seen <= 1'b0;

            // 失效:epoch+1 立刻作废全部行;在飞的请求打标记,装完重来
            if (invalidate) begin
                epoch   <= epoch + {{(EPOCH_W-1){1'b0}}, 1'b1};
                pf_pend <= 1'b0;          // 环里的字节变了, 待预取的行已经没意义
                if (state != C_IDLE) rearm <= 1'b1;
            end

            case (state)
            C_IDLE: begin
                // 优先级: 真实请求 > 失效后重来 > 预取。预取被抢占不会丢, 下次还会挂上
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
                    pf_pend  <= 1'b0;
                    a_line_q <= pf_line_q; idx_q <= cur_idx; tag_q <= cur_tag;
                    beat_q   <= '0;
                    is_pf <= 1'b1; fill_acked <= 1'b0;
                    state <= C_CHK;
                end
            end

            C_CHK: begin
                if (hit) begin
                    if (!is_pf) begin
                        rsp_valid <= 1'b1;
                        rsp_line  <= a_line_q;
                        rsp_data  <= bank_pack;
                        if (pf_ok) begin 
                            pf_pend <= 1'b1; 
                            pf_line_q <= a_line_next; 
                        end
                    end
                    state <= C_IDLE;
                end
                else if (port_free) begin
                    state <= C_MISS;
                end
            end

            // 一个状态里同时等 ack 和数据: 数据按拍进(每拍写一个 bank),
            // 收下请求(ack)就把请求线撤掉。两者互相独立, 不需要两个状态。
            C_MISS: begin
                if (m_rd_vld) begin
                    beat_q <= beat_q + {{(BEAT_W-1){1'b0}}, 1'b1};
                    if (m_rd_last) begin
                        err_q <= m_rd_err;
                        state <= C_RSP;      // 这一拍已发了 SRAM 读, 下一拍就能回
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
                    // 行内最后一个字的 bank 这一拍正在写, 读不回来 ⇒ 用 last_word_q 顶上去
                    rsp_data  <= {last_word_q, bank_pack[BLOCK_SIZE*32-33:0]};
                    rsp_err   <= err_q;
                end
                if (!rearm && !is_pf && !err_q && pf_ok) begin
                    pf_pend <= 1'b1; pf_line_q <= a_line_next;
                end
                state <= C_IDLE;
            end

            default: state <= C_IDLE;
            endcase
        end
    end

    tagv_sram #(
        .WIDTH (TAGV_W    ),
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

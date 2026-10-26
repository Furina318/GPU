`include "defines.vh"

// 校验解析命令

module cmd_decode import cp_pkg::*; (
    input  wire [15:0] len        ,     // length_words
    input  wire [ 7:2] flags      ,     // cmd_flags 的高 6 位(bit0/1 是 SYNC/IRQ_ON_DONE, 不在这里查)
    input  wire [ 7:0] opcode     ,
    input  wire [15:0] signal_id  ,
    input  wire [15:0] wait_id    ,
    input  wire [31:0] cmd_w [0:15],    // 命令体的前 16 个字(w[0] = word0)

    output logic       ok         ,
    output logic [3:0] err        ,     // cp_err_t
    output logic       is_engine  ,     // 转发给 2D/GEMM 的命令
    output logic [1:0] engine     ,     // cp_eng_t
    output logic       is_vendor     
);
    wire [31:0] w2  = cmd_w[2 ];
    wire [31:0] w3  = cmd_w[3 ];
    wire [31:0] w4  = cmd_w[4 ];
    wire [31:0] w5  = cmd_w[5 ];
    wire [31:0] w7  = cmd_w[7 ];
    wire [15:0] w9  = cmd_w[9 ][15:0];   // dtype[7:0] + flags[15:8]
    wire [31:16] w10 = cmd_w[10][31:16]; // 只有高半字是保留位
    wire [31:8] w12 = cmd_w[12][31:8];   // 低字节 requant_shift 归引擎
    wire [31:0] w13 = cmd_w[13];
    wire [31:0] w14 = cmd_w[14];
    wire [31:0] w15 = cmd_w[15];

    /* verilator lint_off UNUSEDSIGNAL */
    wire [15:0] g_m = w5[15:0], g_n = w5[31:16];
    wire [15:0] g_k = cmd_w[6][15:0];
    wire [15:0] g_lda = w7[15:0], g_ldb = w7[31:16], g_ldc = cmd_w[8][15:0];
    wire [ 7:0] g_dtype = w9[7:0], g_gflags = w9[15:8];
    wire        g_bT = g_gflags[1];
    /* verilator lint_on UNUSEDSIGNAL */
    logic bad;

    always_comb begin
        ok        = 1'b1;
        err       = CPERR_NONE;
        is_engine = 1'b0;
        engine    = ENG_NONE;
        is_vendor = (opcode >= 8'h80);

        // 通用头检查
        if (len < 16'd2)                                  bad = 1'b1;   // 命令至少 2 字
        else if (flags != 6'h0)                           bad = 1'b1;   // cmd_flags 保留位(bit2..7)
        else if (signal_id != SEM_NONE && signal_id >= 16'h0100) bad = 1'b1;  // 保留区
        else if (wait_id   != SEM_NONE && wait_id   >= 16'h0100) bad = 1'b1;
        else                                              bad = 1'b0;
        if (bad) begin ok = 1'b0; err = CPERR_CMD; end

        if (ok) begin
            case (opcode)
                // NOP:任意长度 ≥ 2
                `CMD_NOP: ;

                // WRITE_REG:长度 4、地址对齐且在 MMIO 窗口内
                `CMD_WRITE_REG: begin
                    if (len != LEN_WRITE_REG) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w2[1:0] != 2'b00 || w2 >= `MMIO_END) begin
                        ok = 1'b0; err = CPERR_CMD;
                    end
                end

                // READ_REG:长度 4、addr 在 MMIO、dst 对齐且不在 MMIO
                `CMD_READ_REG: begin
                    if (len != LEN_READ_REG) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w2[1:0] != 2'b00 || w2 >= `MMIO_END) begin
                        ok = 1'b0; err = CPERR_CMD;
                    end
                    else if (w3[1:0] != 2'b00 || w3 < `MMIO_END) begin
                        ok = 1'b0; err = CPERR_CMD;
                    end
                end

                // LOAD_KERNEL:长度 5、size/sm_mask/reserved
                `CMD_LOAD_KERNEL: begin
                    if (len != LEN_LOAD_KERNEL) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w4[31:2] != 30'h0) begin ok = 1'b0; err = CPERR_CMD; end  // reserved
                    else if (w4[1:0] == 2'b00)  begin ok = 1'b0; err = CPERR_CMD; end  // sm_mask=0
                    else if (w2[1:0] != 2'b00)  begin ok = 1'b0; err = CPERR_CMD; end  // src 对齐
                    else if (w3 == 32'h0 || w3[1:0] != 2'b00 || w3 > `INSTR_SRAM_SIZE) begin
                        ok = 1'b0; err = CPERR_CMD;                                   // size 4B 倍数且 ≤ 16KB
                    end
                end

                // LAUNCH:长度 5、word3 保留位(其余九条在 cp.sv)
                `CMD_LAUNCH: begin
                    if (len != LEN_LAUNCH)      begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w3[31:8] != 24'h0) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w3[7:0] == 8'h0 || w3[7:0] > 8'h8) begin
                        ok = 1'b0; err = CPERR_CMD;                                  // 1 ≤ num_warps ≤ 8
                    end
                end

                // FENCE:长度 2
                `CMD_FENCE: if (len != LEN_FENCE) begin ok = 1'b0; err = CPERR_CMD; end

                // SIGNAL / WAIT:长度 4、sem_addr 对齐且在全局内存
                `CMD_SIGNAL: begin
                    if (len != LEN_SIGNAL) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w2[1:0] != 2'b00 || w2 < `MMIO_END) begin
                        ok = 1'b0; err = CPERR_CMD;
                    end
                end
                `CMD_WAIT: begin
                    if (len != LEN_SIGNAL) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w2[1:0] != 2'b00 || w2 < `MMIO_END) begin
                        ok = 1'b0; err = CPERR_CMD;
                    end
                end

                // IRQ:长度 3、irq_id ≤ 7、保留位
                `CMD_IRQ: begin
                    if (len != LEN_IRQ)        begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w2[31:8] != 24'h0) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w2[7:0] > 8'd7)    begin ok = 1'b0; err = CPERR_CMD; end
                end

                // PARAMS:长度 4+N、保留位、dst_off/边界
                `CMD_PARAMS: begin
                    if (w2[31:16] != 16'h0 || w3[31:16] != 16'h0) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w2[1:0] != 2'b00 || w2[15:0] >= CONST_SIZE[15:0]) begin
                        ok = 1'b0; err = CPERR_CMD;
                    end
                    else if (w3[15:0] < 16'd1 || w3[15:0] > 16'd256) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (len != (16'd4 + w3[15:0])) begin ok = 1'b0; err = CPERR_CMD; end
                    else if ({16'h0, w2[15:0]} + {16'h0, w3[15:0]} * 32'd4 > CONST_SIZE) begin
                        ok = 1'b0; err = CPERR_CMD;                                   // 越出 4KB 常量区
                    end
                end

                // GEMM:长度 16 + §2.4 的全部校验
                `CMD_GEMM: begin
                    is_engine = 1'b1; engine = ENG_GEMM;
                    if (len != LEN_GEMM)                          begin ok = 1'b0; err = CPERR_CMD; end
                    else if (((w2 | w3 | w4) & 32'h0000_000F) != 32'h0) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (g_m == 0 || g_m > 16'd4096 ||
                             g_n == 0 || g_n > 16'd4096 ||
                             g_k == 0 || g_k > 16'd4096)          begin ok = 1'b0; err = CPERR_CMD; end
                    else if (w10 != 0 || w12 != 0 ||
                             w13 != 0 || w14 != 0 || w15 != 0)    begin ok = 1'b0; err = CPERR_CMD; end
                    else if (g_dtype != 8'h0)                      begin ok = 1'b0; err = CPERR_CMD; end // v1 只支持 INT8
                    else if (g_gflags[2])                          begin ok = 1'b0; err = CPERR_CMD; end // REQUANT 未实现
                    else if (g_gflags[7:3] != 5'h0)                begin ok = 1'b0; err = CPERR_CMD; end
                    else if (g_lda != 0 && g_lda < g_k)            begin ok = 1'b0; err = CPERR_CMD; end
                    else if (g_ldb != 0 && g_ldb < (g_bT ? g_k : g_n)) begin ok = 1'b0; err = CPERR_CMD; end
                    else if (g_ldc != 0 && g_ldc < g_n)            begin ok = 1'b0; err = CPERR_CMD; end
                end

                // 2D(0x10..0x17):长度必须等于固定值;参数由引擎校验
                `CMD_2D_FILL, `CMD_2D_BLIT, `CMD_2D_DRAW,
                `CMD_2D_SET_REG, `CMD_2D_FLUSH: begin
                    is_engine = 1'b1; engine = ENG_2D;
                    case (opcode)
                        `CMD_2D_FILL:    if (len != 16'd6) begin ok = 1'b0; err = CPERR_CMD; end
                        `CMD_2D_BLIT:    if (len != 16'd9) begin ok = 1'b0; err = CPERR_CMD; end
                        `CMD_2D_DRAW:    if (len != 16'd8) begin ok = 1'b0; err = CPERR_CMD; end
                        `CMD_2D_SET_REG: if (len != 16'd4) begin ok = 1'b0; err = CPERR_CMD; end
                        `CMD_2D_FLUSH:   if (len != 16'd2) begin ok = 1'b0; err = CPERR_CMD; end
                        default: ;
                    endcase
                end

                // ── 0x15..0x17 保留给 2D:长度 ≥ 2 即转交, 由引擎报 BAD_PARAM ──
                8'h15, 8'h16, 8'h17: begin
                    is_engine = 1'b1; engine = ENG_2D;
                end

                // 厂商自定义(0x80..0xFF):按长度跳过
                default: begin
                    if (is_vendor) ;                                  // 合法, 跳过
                    else begin ok = 1'b0; err = CPERR_CMD; end         // 0x0A..0x0F/0x19..0x7F 保留
                end
            endcase
        end
    end
endmodule

`include "fpu_config.vh"

module fpu #(
    parameter ID_WIDTH = 5
)(
    input  wire                clk        ,
    input  wire                rst        ,
    
    input  wire                valid      ,
    output wire                ready      ,
    input  wire [         6:0] fpu_op     ,
    input  wire [         2:0] rm         ,
    input  wire [        31:0] src1       ,
    input  wire [        31:0] src2       ,
    input  wire [        31:0] src3       ,

    input  wire                flush      ,
    input  wire [ID_WIDTH-1:0] flush_id   ,

    // 乱序核传入的指令 ID，写回时原样带回
    input  wire [ID_WIDTH-1:0] fpu_id     ,
    
    output wire                fpu_valid  ,
    output wire [ID_WIDTH-1:0] fpu_id_o   ,
    output wire [        31:0] fpu_result ,
    output wire [         4:0] fpu_fflags
);

    wire is_fmac = (fpu_op[6:4] == 3'b001);

    wire is_fsgnj   = (fpu_op == `FSGNJ_S );
    wire is_fsgnjn  = (fpu_op == `FSGNJN_S);
    wire is_fsgnjx  = (fpu_op == `FSGNJX_S);
    wire is_fsgnj_all = (fpu_op[6:3] == 4'b0101);

    wire is_fclass = (fpu_op == `FCLASS_S);
    wire is_fmin   = (fpu_op == `FMIN_S  );
    wire is_fmax   = (fpu_op == `FMAX_S  );
    wire is_flt    = (fpu_op == `FLT_S   );
    wire is_feq    = (fpu_op == `FEQ_S   );
    wire is_fle    = (fpu_op == `FLE_S   );
    wire is_fcmp   = is_flt | is_feq | is_fle;
    wire is_fquick  = (fpu_op[6:4] == 3'b010);
    wire [8:0] fquick_op = {is_fsgnjx, is_fsgnjn, is_fsgnj, is_fle, is_flt, is_feq, is_fclass, is_fmin, is_fmax};

    wire is_fcvt_w_s  = (fpu_op == `FCVT_W_S );
    wire is_fcvt_wu_s = (fpu_op == `FCVT_WU_S);
    wire is_fcvt_s_w  = (fpu_op == `FCVT_S_W );
    wire is_fcvt_s_wu = (fpu_op == `FCVT_S_WU);
    wire is_fcvt = (fpu_op[6:4] == 3'b011);

    wire is_fdiv  = (fpu_op == `FDIV_S );
    wire is_fsqrt = (fpu_op == `FSQRT_S);


    wire               sign_a, sign_b, sign_c;
    wire signed [ 8:0] exp_a, exp_b, exp_c;
    wire        [23:0] sig_a, sig_b, sig_c;
    wire        zero_a, subnormal_a, normal_a, inf_a, nan_a, snan_a, qnan_a;
    wire        zero_b, subnormal_b, normal_b, inf_b, nan_b, snan_b, qnan_b;
    wire        zero_c, inf_c, nan_c, snan_c;

    fpu_decoder u_dec_a (
        .a            (src1        ),
        .sign         (sign_a      ),
        .exp          (exp_a       ),
        .sig          (sig_a       ),
        .is_zero      (zero_a      ),
        .is_subnormal (subnormal_a ),
        .is_normal    (normal_a    ),
        .is_inf       (inf_a       ),
        .is_nan       (nan_a       ),
        .is_snan      (snan_a      ),
        .is_qnan      (qnan_a      )
    );

    fpu_decoder u_dec_b (
        .a            (src2        ),
        .sign         (sign_b      ),
        .exp          (exp_b       ),
        .sig          (sig_b       ),
        .is_zero      (zero_b      ),
        .is_subnormal (subnormal_b ),
        .is_normal    (normal_b    ),
        .is_inf       (inf_b       ),
        .is_nan       (nan_b       ),
        .is_snan      (snan_b      ),
        .is_qnan      (qnan_b      )
    );

    // src3 仅 fma 使用，只取 fma_unit 需要的字段
    wire dec_c_sub_unused, dec_c_norm_unused, dec_c_qnan_unused;
    fpu_decoder u_dec_c (
        .a            (src3             ),
        .sign         (sign_c           ),
        .exp          (exp_c            ),
        .sig          (sig_c            ),
        .is_zero      (zero_c           ),
        .is_subnormal (dec_c_sub_unused ),
        .is_normal    (dec_c_norm_unused),
        .is_inf       (inf_c            ),
        .is_nan       (nan_c            ),
        .is_snan      (snan_c           ),
        .is_qnan      (dec_c_qnan_unused)
    );

    // 浮点乘-加融合单元
    wire        fma_busy;
    wire        fma_wb_valid;
    wire [ID_WIDTH-1:0] fma_wb_id;
    wire [31:0] fma_result;
    wire [ 4:0] fma_fflags;

    fma_unit #(
        .ID_WIDTH (ID_WIDTH)
    ) u_fma_unit (
        .clk         (clk            ),
        .rst         (rst            ),
        .issue_valid (valid & ready & is_fmac),
        .issue_id    (fpu_id         ),
        .op          (fpu_op         ),
        .rm          (rm             ),
        .s1_sign     (sign_a         ),
        .s1_exp      (exp_a          ),
        .s1_sig      (sig_a          ),
        .s1_zero     (zero_a         ),
        .s1_inf      (inf_a          ),
        .s1_nan      (nan_a          ),
        .s1_snan     (snan_a         ),
        .s2_sign     (sign_b         ),
        .s2_exp      (exp_b          ),
        .s2_sig      (sig_b          ),
        .s2_zero     (zero_b         ),
        .s2_inf      (inf_b          ),
        .s2_nan      (nan_b          ),
        .s2_snan     (snan_b         ),
        .s3_sign     (sign_c         ),
        .s3_exp      (exp_c          ),
        .s3_sig      (sig_c          ),
        .s3_zero     (zero_c         ),
        .s3_inf      (inf_c          ),
        .s3_nan      (nan_c          ),
        .s3_snan     (snan_c         ),
        .flush       (flush          ),
        .flush_id    (flush_id       ),
        .wb_ack      (arb_grant[0]   ),
        .busy_o      (fma_busy        ),
        .wb_valid    (fma_wb_valid   ),
        .wb_id       (fma_wb_id      ),
        .wb_result   (fma_result     ),
        .wb_fflags   (fma_fflags     )
    );

    // 快速运算单元
    wire [31:0] fquick_result;
    wire [4:0]  fquick_fflags;

    fquick u_fquick (
        .a            (src1        ),
        .a_sign       (sign_a      ),
        .a_exp        (exp_a       ),
        .a_sig        (sig_a       ),
        .a_is_zero    (zero_a      ),
        .a_is_subnormal(subnormal_a),
        .a_is_normal  (normal_a    ),
        .a_is_inf     (inf_a       ),
        .a_is_nan     (nan_a       ),
        .a_is_snan    (snan_a      ),
        .a_is_qnan    (qnan_a      ),

        .b            (src2        ),
        .b_sign       (sign_b      ),
        .b_exp        (exp_b       ),
        .b_sig        (sig_b       ),
        .b_is_zero    (zero_b      ),
        .b_is_subnormal(subnormal_b),
        .b_is_normal  (normal_b    ),
        .b_is_inf     (inf_b       ),
        .b_is_nan     (nan_b       ),
        .b_is_snan    (snan_b      ),
        .b_is_qnan    (qnan_b      ),

        .fquick_op    (fquick_op    ),
        .result       (fquick_result),
        .fflags       (fquick_fflags)
    );

    // 比较指令的 NV 由顶层单独生成
    wire [4:0] fquick_fflags_final =
        is_fcmp ? (is_feq ? {(snan_a | snan_b), 4'b0000} :
                            {(nan_a | nan_b), 4'b0000}) :
                  fquick_fflags;

    // 转换单元
    wire [31:0] fcvt_result;
    wire [4:0]  fcvt_fflags;

    fcvt u_fcvt (
        .src            (src1        ),
        .src_sign       (sign_a      ),
        .src_exp        (exp_a       ),
        .src_sig        (sig_a       ),
        .src_is_zero    (zero_a      ),
        .src_is_subnormal(subnormal_a),
        .src_is_normal  (normal_a    ),
        .src_is_inf     (inf_a       ),
        .src_is_nan     (nan_a       ),
        .src_is_snan    (snan_a      ),
        .src_is_qnan    (qnan_a      ),
        .rm             (rm          ),
        .is_w_s         (is_fcvt_w_s ),
        .is_wu_s        (is_fcvt_wu_s),
        .is_s_w         (is_fcvt_s_w ),
        .is_s_wu        (is_fcvt_s_wu),
        .result         (fcvt_result ),
        .fflags         (fcvt_fflags )
    );

    // 除法单元（单拍组合除法 + 下一拍锁存写回）
    wire        fdiv_busy;
    wire        fdiv_wb_valid;
    wire [ID_WIDTH-1:0] fdiv_wb_id;
    wire [31:0] fdiv_result;
    wire [ 4:0] fdiv_fflags;

    fdiv #(
        .ID_WIDTH (ID_WIDTH)
    ) u_fdiv (
        .clk         (clk            ),
        .rst         (rst            ),
        .issue_valid (valid & ready & is_fdiv),
        .issue_id    (fpu_id         ),
        .op          (fpu_op         ),
        .rm          (rm             ),
        .s1_sign     (sign_a         ),
        .s1_exp      (exp_a          ),
        .s1_sig      (sig_a          ),
        .s1_zero     (zero_a         ),
        .s1_inf      (inf_a          ),
        .s1_nan      (nan_a          ),
        .s1_snan     (snan_a         ),
        .s2_sign     (sign_b         ),
        .s2_exp      (exp_b          ),
        .s2_sig      (sig_b          ),
        .s2_zero     (zero_b         ),
        .s2_inf      (inf_b          ),
        .s2_nan      (nan_b          ),
        .s2_snan     (snan_b         ),
        .flush       (flush          ),
        .flush_id    (flush_id       ),
        .busy_o      (fdiv_busy      ),
        .wb_ack      (arb_grant[3]   ),
        .wb_valid    (fdiv_wb_valid  ),
        .wb_id       (fdiv_wb_id     ),
        .wb_result   (fdiv_result    ),
        .wb_fflags   (fdiv_fflags    )
    );

    // 平方根单元
    wire        fsqrt_busy;
    wire        fsqrt_wb_valid;
    wire [ID_WIDTH-1:0] fsqrt_wb_id;
    wire [31:0] fsqrt_result;
    wire [ 4:0] fsqrt_fflags;

    fsqrt #(
        .ID_WIDTH (ID_WIDTH)
    ) u_fsqrt (
        .clk         (clk            ),
        .rst         (rst            ),
        .issue_valid (valid & ready & is_fsqrt),
        .issue_id    (fpu_id         ),
        .rm          (rm             ),
        .s1_sign     (sign_a         ),
        .s1_exp      (exp_a          ),
        .s1_sig      (sig_a          ),
        .s1_zero     (zero_a         ),
        .s1_inf      (inf_a          ),
        .s1_nan      (nan_a          ),
        .s1_snan     (snan_a         ),
        .flush       (flush          ),
        .flush_id    (flush_id       ),
        .busy_o      (fsqrt_busy     ),
        .wb_ack      (arb_grant[4]   ),
        .wb_valid    (fsqrt_wb_valid ),
        .wb_id       (fsqrt_wb_id    ),
        .wb_result   (fsqrt_result   ),
        .wb_fflags   (fsqrt_fflags   )
    );

    // 写回仲裁,年龄优先
    // fquick 和 fcvt指令保留
    reg                qk_pend_valid;
    reg [ID_WIDTH-1:0] qk_pend_id;
    reg [31:0]         qk_pend_data;
    reg [4:0]          qk_pend_fflags;

    reg                cv_pend_valid;
    reg [ID_WIDTH-1:0] cv_pend_id;
    reg [31:0]         cv_pend_data;
    reg [4:0]          cv_pend_fflags;

    // 与 fma_unit 一致的冲刷判定: flush 取消 id 比 flush_id 更年轻的指令;
    // 仲裁器自身是按 id == flush_id 屏蔽的, 所以这里两者都覆盖。
    function automatic id_flushed;
        input [ID_WIDTH-1:0] a;
        input                fl;
        input [ID_WIDTH-1:0] fid;
        reg                  younger;
        begin
            younger = (a != fid) &&
                ((a[ID_WIDTH-1] ^ fid[ID_WIDTH-1]) ? (a[ID_WIDTH-2:0] < fid[ID_WIDTH-2:0])
                                                   : (a[ID_WIDTH-2:0] > fid[ID_WIDTH-2:0]));
            id_flushed = fl && ((a == fid) || younger);
        end
    endfunction

    // 写回确认: 仲裁器的授予向量, 按端口编号对齐 arb_valid
    wire qk_ack = arb_grant[1];
    wire cv_ack = arb_grant[2];

    wire qk_issue = valid & ready & is_fquick & ~flush;
    wire cv_issue = valid & ready & is_fcvt   & ~flush;

    always @(posedge clk) begin
        if (rst) begin
            qk_pend_valid  <= 1'b0;
            qk_pend_id     <= {ID_WIDTH{1'b0}};
            qk_pend_data   <= 32'd0;
            qk_pend_fflags <= 5'd0;
            cv_pend_valid  <= 1'b0;
            cv_pend_id     <= {ID_WIDTH{1'b0}};
            cv_pend_data   <= 32'd0;
            cv_pend_fflags <= 5'd0;
        end else begin
            // 被冲刷的结果直接丢弃
            if (id_flushed(qk_pend_id, flush, flush_id)) qk_pend_valid <= 1'b0;
            if (id_flushed(cv_pend_id, flush, flush_id)) cv_pend_valid <= 1'b0;
            // 被仲裁器取走则释放
            if (qk_ack) qk_pend_valid <= 1'b0;
            if (cv_ack) cv_pend_valid <= 1'b0;
            // 新结果锁存, 放在最后, 使"同拍释放并接收"正确生效
            if (qk_issue) begin
                qk_pend_valid  <= 1'b1;
                qk_pend_id     <= fpu_id;
                qk_pend_data   <= fquick_result;
                qk_pend_fflags <= fquick_fflags_final;
            end
            if (cv_issue) begin
                cv_pend_valid  <= 1'b1;
                cv_pend_id     <= fpu_id;
                cv_pend_data   <= fcvt_result;
                cv_pend_fflags <= fcvt_fflags;
            end
        end
    end

    // 只有"空闲 或 本拍被取走"时才允许接收新结果, 否则会覆盖尚未写回的旧值
    wire qk_free = ~qk_pend_valid | qk_ack;
    wire cv_free = ~cv_pend_valid | cv_ack;

    wire fquick_wb_valid = qk_pend_valid;
    wire fcvt_wb_valid   = cv_pend_valid;

    wire [4:0] arb_valid = {fsqrt_wb_valid, fdiv_wb_valid, fcvt_wb_valid, fquick_wb_valid, fma_wb_valid};

    wire [5*32-1:0]        arb_data;
    wire [5*5-1:0]         arb_fflags;
    wire [5*ID_WIDTH-1:0]  arb_id;

    assign arb_data[32*0 +: 32]   = fma_result;
    assign arb_data[32*1 +: 32]   = qk_pend_data;
    assign arb_data[32*2 +: 32]   = cv_pend_data;
    assign arb_data[32*3 +: 32]   = fdiv_result;
    assign arb_data[32*4 +: 32]   = fsqrt_result;

    assign arb_fflags[5*0 +: 5] = fma_fflags;
    assign arb_fflags[5*1 +: 5] = qk_pend_fflags;
    assign arb_fflags[5*2 +: 5] = cv_pend_fflags;
    assign arb_fflags[5*3 +: 5] = fdiv_fflags;
    assign arb_fflags[5*4 +: 5] = fsqrt_fflags;

    assign arb_id[ID_WIDTH*0 +: ID_WIDTH] = fma_wb_id;
    assign arb_id[ID_WIDTH*1 +: ID_WIDTH] = qk_pend_id;
    assign arb_id[ID_WIDTH*2 +: ID_WIDTH] = cv_pend_id;
    assign arb_id[ID_WIDTH*3 +: ID_WIDTH] = fdiv_wb_id;
    assign arb_id[ID_WIDTH*4 +: ID_WIDTH] = fsqrt_wb_id;

    // 授予向量同时就是各写回源的确认信号: grant_o 只在 best_valid 时非零,
    // 所以 grant[i] 蕴含 wb_valid_o=1。
    wire [4:0] arb_grant;
    fpu_arbiter #(
        .NUM_PORTS  (5),
        .ID_WIDTH   (ID_WIDTH),
        .DATA_WIDTH (32)
    ) u_fpu_arbiter (
        .req_valid_i (arb_valid       ),
        .req_id_i    (arb_id          ),
        .req_data_i  (arb_data        ),
        .req_fflags_i(arb_fflags      ),
        .grant_o     (arb_grant       ),
        .wb_valid_o  (fpu_valid       ),
        .wb_id_o     (fpu_id_o        ),
        .wb_data_o   (fpu_result      ),
        .wb_fflags_o (fpu_fflags      ),
        .flush       (flush           ),
        .flush_id    (flush_id        )
    );

    wire op_is_div  = (fpu_op == `FDIV_S );
    wire op_is_sqrt = (fpu_op == `FSQRT_S);

    assign ready = (~op_is_div  | ~fdiv_busy ) &
                   (~op_is_sqrt | ~fsqrt_busy) &
                   ~fma_busy &
                   qk_free & cv_free;

endmodule

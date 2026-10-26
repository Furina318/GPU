`include "defines.vh"

//  fetch —— 每 warp 一个的取指单元(PC + 指令 SRAM 接口 + 控制流收口)
module fetch (
    input  wire                   clk            ,
    input  wire                   rst            ,
    input  wire                   halt           ,   // SM 级停机; 锁存, 由 rst 解除
    // CP LAUNCH
    input  wire                   launch         ,
    input  wire [31:0]            launch_pc      ,
    input  wire [`WARP_SIZE-1:0]  launch_warp    ,
    // 上游(调度器)
    input  wire                   fet_in_valid   ,
    output wire                   fet_in_ready   ,
    // 下游(译码)
    output wire                   fet_out_valid  ,
    input  wire                   fet_out_ready  ,
    output reg  [31:0]            pc             ,
    output reg  [31:0]            inst           ,
    // lane 掩码
    output reg  [`WARP_SIZE-1:0]  inst_mask      ,

    input  wire  [31:0]           target_pc      , 
    input  wire                   target_valid   ,
    input  wire  [`WARP_SIZE-1:0] target_taken   ,   // 谓词为真的 lane

    output wire                   imem_req_valid ,
    input  wire                   imem_req_ready ,   // 取指口仲裁授予(请求被接受)
    output wire [31:0]            imem_req_pc    ,
    input  wire                   imem_rsp_valid ,
    input  wire [31:0]            imem_rsp_inst  ,
    // 状态输出(SM / 调度器 / MMIO 观测)
    output wire                   br_pending     ,   // 正在等 EXU 回送分支判定
    output wire                   br_local       ,   // 本 warp 就地判定了 @p0 分支(脉冲)
    output wire                   br_resolved    ,   // 分支判定已被接收(脉冲, 回执给 EXU)
    output wire [`WARP_SIZE-1:0]  active_mask    ,   // 当前 lane 掩码
    output wire                   stack_empty    ,   // bar.sync 的 BAR_DIVERGENT 检查用(§2.4)
    output wire [3:0]             stack_depth    ,
    output wire [31:0]            fault_pc       ,   // 首个错误的 PC(CMDS §7 的 FAULT_PC)
    output wire                   err_imem       ,   // PC 非法(未 4B 对齐 / 超出指令 SRAM)
    output wire                   err_no_recov   ,   // ISA §2.6
    output wire                   err_simt_ovf   ,   // ISA §2.6
    output wire                   err_internal   ,   // 收敛循环失控(实现自检)
    output wire                   halted             // 停机(错误或外部 halt)
);

    typedef enum logic [1:0] {
        S_IDLE    = 2'b00,   // 等调度器授权
        S_RESP    = 2'b01,   // 等指令 SRAM 回读
        S_SENT    = 2'b10,   // 指令送译码
        S_BR_WAIT = 2'b11    // 等 EXU 回送 br.simt 判定
    } fetch_state_t;

    fetch_state_t state;
    reg           br_pending_q;
    reg           started_q;      // LAUNCH 过之后才允许取指

    wire [ 6:0] opcode     = inst[6:0];
    wire        is_branch  = (opcode == 7'b1100011);                  
    wire        is_ipdom   = (opcode == 7'b1010111);                     
    wire        br_inv     = inst[28];     // 谓词取反
    wire [ 1:0] br_psel    = inst[27:26];                             
    wire        br_uniform = (br_psel == 2'b00);  // 与 lane 无关
    wire [31:0] imm_br     = {{8{inst[31]}} , inst[31:29], inst[25:7] , 2'b00}; 
    wire [31:0] imm_ipdom  = {{10{inst[31]}}, inst[31:26], inst[25:12], 2'b00}; 

    wire [31:0] pc_next    = pc + 32'd4;

    // PC 合法性
    wire        pc_ok      = (pc[1:0] == 2'b00) && (pc < `INSTR_SRAM_SIZE);

    wire sent_handshake  = fet_out_valid & fet_out_ready;        // 指令被译码接收
    wire do_ipdom    = sent_handshake & is_ipdom;                
    wire do_br_local = sent_handshake & is_branch & br_uniform;  // @p0 分支: 前端就地判定
    // EXU 回送的分支判定
    wire do_br_ext   = ~halted & ((sent_handshake & is_branch & ~br_uniform & target_valid)
                                  | (br_pending_q & target_valid));
    wire do_plain    = sent_handshake & ~is_branch & ~is_ipdom;  // 其余顺序推进

    wire step_valid  = do_ipdom | do_br_local | do_br_ext | do_plain;
    wire step_br     = do_br_local | do_br_ext;

    wire [`WARP_SIZE-1:0] br_local_taken = br_inv ? {`WARP_SIZE{1'b0}} : active_mask;

    // SIMT 栈
    wire [31:0]           stack_next_pc;
    wire [`WARP_SIZE-1:0] stack_mask_next;
    wire [31:0]           recov_pc_w;
    wire                  recov_valid_w;

    simt_stack u_simt_stack (
        .clk              (clk                        ),
        .rst              (rst                        ),
        .launch           (launch                     ),
        .launch_mask      (launch_warp                ),
        .ipdom_valid      (do_ipdom                   ),
        .ipdom_recov_pc   (pc + imm_ipdom             ),   
        .br_valid         (step_br                    ),
        .br_target_pc     (do_br_ext ? target_pc : (pc + imm_br)),
        .br_fall_pc       (pc_next                    ),   
        .br_taken_mask    (do_br_ext ? target_taken : br_local_taken),
        .step_valid       (step_valid                 ),
        .next_pc_in       (pc_next                    ),
        .next_pc_out      (stack_next_pc              ),
        .active_mask      (active_mask                ),
        .active_mask_next (stack_mask_next            ),
        .stack_empty      (stack_empty                ),
        .stack_depth      (stack_depth                ),
        .recov_valid      (recov_valid_w              ),
        .recov_pc         (recov_pc_w                 ),
        .err_no_recov     (err_no_recov               ),
        .err_simt_ovf     (err_simt_ovf               ),
        .err_internal     (err_internal               )
    );

    `UNUSED_VAR(recov_pc_w)
    `UNUSED_VAR(recov_valid_w)

    // 错误与停机 
    wire stack_err    = err_no_recov | err_simt_ovf | err_internal;
    wire err_imem_now = fet_in_ready & fet_in_valid & ~pc_ok;   // 授权到手才发现 PC 非法
    wire err_any      = err_imem_now | stack_err;

    reg        halted_q;
    reg        fault_valid;
    reg [31:0] fault_pc_q;

    assign halted   = halt | halted_q;
    assign err_imem = err_imem_now;
    assign fault_pc = fault_pc_q;

    // 错误现场处理
    always_ff @(posedge clk) begin
        if (rst) begin
            halted_q    <= 1'b0;
            fault_valid <= 1'b0;
            fault_pc_q  <= 32'h0;
        end
        else begin
            if (err_any & ~fault_valid) begin
                fault_valid <= 1'b1;
                fault_pc_q  <= pc;
            end
            if (err_any | halt) halted_q <= 1'b1;
        end
    end

    assign fet_in_ready   = (state == S_IDLE) & ~halted & ~launch & started_q;
    assign imem_req_valid = (state == S_IDLE) & fet_in_valid & fet_in_ready & pc_ok;
    assign imem_req_pc    = pc;
    assign fet_out_valid  = (state == S_SENT) & ~halted;
    assign br_pending     = br_pending_q;
    assign br_local       = do_br_local;
    assign br_resolved    = do_br_ext;         // 保持型 target_valid 必须看到它就撤掉

    // PC/inst/掩码 更新
    always_ff @(posedge clk) begin
        if (rst) begin
            state        <= S_IDLE;
            pc           <= `RESET_PC;
            inst         <= 32'h0;
            inst_mask    <= {`WARP_SIZE{1'b1}};
            br_pending_q <= 1'b0;
            started_q    <= 1'b0;
        end
        else if (launch) begin
            state        <= S_IDLE;
            pc           <= launch_pc;               // CP LAUNCH 给出的 kernel 入口
            inst         <= 32'h0;
            inst_mask    <= launch_warp;
            br_pending_q <= 1'b0;
            started_q    <= 1'b1;
        end
        else if (~halted) begin
            unique case (state)
                S_IDLE: begin
                    // 请求必须被取指口仲裁接受才算发出, 否则留在 S_IDLE(请求电平保持)
                    if (fet_in_valid & fet_in_ready & pc_ok & imem_req_ready)
                        state <= S_RESP;         
                end

                S_RESP: begin
                    if (imem_rsp_valid) begin
                        inst  <= imem_rsp_inst;
                        state <= S_SENT;
                    end
                end

                S_SENT: begin
                    if (sent_handshake) begin
                        if ((is_branch & ~br_uniform) & ~target_valid) begin
                            // 条件依赖每 lane 的谓词值，等 EXU 回送; 期间 PC/掩码/栈全不动
                            br_pending_q <= 1'b1;
                            state        <= S_BR_WAIT;
                        end
                        else begin
                            pc        <= stack_next_pc;
                            inst_mask <= stack_mask_next;
                            state     <= S_IDLE;
                        end
                    end
                end

                S_BR_WAIT: begin
                    if (target_valid) begin
                        br_pending_q <= 1'b0;
                        if (stack_err) begin
                            state <= S_BR_WAIT;
                        end
                        else begin
                            pc        <= stack_next_pc;
                            inst_mask <= stack_mask_next;
                            state     <= S_IDLE;
                        end
                    end
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule

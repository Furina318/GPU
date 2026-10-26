`include "defines.vh"

//  simt_stack —— 每 warp 一套的 SIMT 栈 + 发散/收敛状态机

module simt_stack #(
    parameter int DEPTH = `SIMT_STACK_DEPTH, 
    parameter int LANES = `WARP_SIZE          
) (
    input  wire                  clk             ,
    input  wire                  rst             ,
    // 内核启动
    input  wire                  launch          ,
    input  wire [LANES-1:0]      launch_mask     ,
    // 写 recov 寄存器; 只对紧随其后的第一条 br.simt 生效
    input  wire                  ipdom_valid     ,
    input  wire [31:0]           ipdom_recov_pc  ,
    // br.simt 解析结果
    input  wire                  br_valid        ,
    input  wire [31:0]           br_target_pc    ,   
    input  wire [31:0]           br_fall_pc      ,   // 未跳转顺序pc
    input  wire [LANES-1:0]      br_taken_mask   ,   // 谓词为真的 lane(effective mask 内)
    // 本周期要推进的指令: 候选下一条 PC(顺序 pc+4, 或分支结果)
    input  wire                  step_valid      ,
    input  wire [31:0]           next_pc_in      ,
    // 收敛处理后真正的下一条 PC
    output logic [31:0]          next_pc_out     ,
    output logic [LANES-1:0]     active_mask     ,   // 当前 lane 掩码
    output logic [LANES-1:0]     active_mask_next,   // step 之后应用于后继指令的掩码
    output logic                 stack_empty     ,
    output logic [3:0]           stack_depth     ,   
    // 调试
    output logic                 recov_valid     ,
    output logic [31:0]          recov_pc        ,
    // 错误事件
    output logic                 err_no_recov    ,   // 发散但 recov 有效位为 0
    output logic                 err_simt_ovf    ,   // 压栈时栈深已达 DEPTH
    output logic                 err_internal        // 收敛循环失控(实现自检, 非内核可触发)
);

    localparam int SPW  = $clog2(DEPTH + 1);  
    localparam int IDXW = (DEPTH < 2) ? 1 : $clog2(DEPTH);

    // 栈帧
    typedef struct packed {
        logic [31:0]      recov_pc    ;   // 收敛点地址(来自 ipdom)
        logic [31:0]      fall_pc     ;   // 分支指令的 PC+4
        logic [LANES-1:0] mask_full   ;   // 发散前的 active_mask
        logic [LANES-1:0] mask_pending;   // 尚未执行的 lane 集合
        logic             pending     ;   // 是否还有一条路径未完成
    } simt_frame_t;

    // _q当前寄存器的值，_c下一拍的值
    simt_frame_t      frames_q [0:DEPTH-1];
    logic [SPW-1:0]   sp_q;
    logic [LANES-1:0] mask_q;
    logic [31:0]      recov_pc_q;
    logic             recov_valid_q;

    simt_frame_t      push_frame_c;
    simt_frame_t      top_view;     // 栈顶视图
    logic [SPW-1:0]   sp_c;         // 栈指针
    logic [LANES-1:0] mask_c; 
    logic [31:0]      np_c;         // next_pc
    logic [31:0]      recov_pc_c;
    logic             recov_valid_c;
    logic [LANES-1:0] taken_c, not_taken_c; // 跳转or未跳转lane几何
    logic [IDXW-1:0]  top_idx, pend_idx_c;
    logic             push_c;      // 压栈使能
    logic             diverge_c;   // 发散使能
    logic             run_c;       // 收敛循环推进标志
    logic             pend_wr_c;
    logic             no_recov_c, ovf_c, internal_c;
    integer           i;

    always_comb begin
        push_frame_c  = frames_q[0];        // 仅占位: push_c=0 时不会被写回
        top_view      = frames_q[0];
        sp_c          = sp_q;
        mask_c        = mask_q;
        np_c          = next_pc_in;
        recov_pc_c    = recov_pc_q;
        recov_valid_c = recov_valid_q;
        push_c        = 1'b0;
        diverge_c     = 1'b0;
        no_recov_c    = 1'b0;
        ovf_c         = 1'b0;
        internal_c    = 1'b0;
        run_c         = 1'b0;
        top_idx       = '0;
        pend_idx_c    = '0;
        pend_wr_c     = 1'b0;

        taken_c     = br_taken_mask & mask_q;
        not_taken_c = mask_q & ~taken_c;

        if (ipdom_valid) begin
            recov_pc_c    = ipdom_recov_pc;
            recov_valid_c = 1'b1;
        end

        // br.simt处理
        if (step_valid && br_valid) begin
            if (taken_c == '0) begin                       // 均匀不跳: PC ← PC+4, 不动栈
                np_c = br_fall_pc;
            end
            else if (not_taken_c == '0) begin              // 均匀跳转: PC ← label, 不动栈
                np_c = br_target_pc;
            end
            else begin                                     // 发散: 压栈 + 切 mask + 跳转
                diverge_c = 1'b1;
                if (!recov_valid_q) begin
                    no_recov_c = 1'b1;                     // ERR_NO_RECOV
                    np_c       = br_fall_pc;
                end
                else if (sp_q >= SPW'(DEPTH)) begin
                    ovf_c = 1'b1;                          // SIMT_OVF(栈已满)
                    np_c  = br_fall_pc;
                end
                else begin
                    push_frame_c.recov_pc     = recov_pc_q;
                    push_frame_c.fall_pc      = br_fall_pc;
                    push_frame_c.mask_full    = mask_q;
                    push_frame_c.mask_pending = not_taken_c;
                    push_frame_c.pending      = 1'b0;
                    push_c = 1'b1;
                    np_c   = br_target_pc;                 // taken 路径恒先执行
                    mask_c = taken_c;
                end
            end
            recov_valid_c = 1'b0;                          // recov 用后即清
        end

        if (push_c)
            sp_c = sp_q + 1'b1;                 // 新帧在时序块里写进 frames_q[sp_q]

        // 收敛推进: 栈非空 且 PC == 栈顶 recov_pc
        if (step_valid && !no_recov_c && !ovf_c) begin
            run_c = 1'b1;
            for (i = 0; i <= DEPTH; i = i + 1) begin
                top_idx = sp_c[IDXW-1:0] - 1'b1;          // 栈顶槽号(sp_c != 0 时)
                // 栈顶视图: 本拍刚压入的帧要优先(它可能立刻匹配收敛点)
                if (push_c && (sp_c == (sp_q + 1'b1))) top_view = push_frame_c;
                else                                   top_view = frames_q[top_idx];

                if (run_c && (sp_c != '0) && (top_view.recov_pc == np_c)) begin
                    if (top_view.pending) begin
                        // 第二条路径到达收敛点: 弹栈, PC 保持 recov_pc ⇒ 完整 mask 执行一次
                        mask_c = top_view.mask_full;
                        sp_c   = sp_c - 1'b1;
                    end
                    else if (top_view.fall_pc == top_view.recov_pc) begin
                        // 空路径特例(fall_pc 就是收敛点): 也直接弹栈, 完整 mask 继续
                        mask_c = top_view.mask_full;
                        sp_c   = sp_c - 1'b1;
                    end
                    else begin
                        // taken 路径到达收敛点: 切到未跳转路径, **此刻不执行收敛点**
                        if (push_c && (sp_c == (sp_q + 1'b1))) begin
                            push_frame_c.pending = 1'b1;
                        end
                        else begin
                            pend_wr_c  = 1'b1;
                            pend_idx_c = top_idx;
                        end
                        mask_c = top_view.mask_pending;
                        np_c   = top_view.fall_pc;
                    end
                end
                else begin
                    run_c = 1'b0;
                end
            end
            if (run_c) internal_c = 1'b1;   // 循环跑满仍在推进
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            sp_q          <= '0;
            mask_q        <= {LANES{1'b1}};
            recov_pc_q    <= 32'h0;
            recov_valid_q <= 1'b0;
        end
        else if (launch) begin        // 清栈             
            sp_q          <= '0;
            mask_q        <= launch_mask;
            recov_pc_q    <= 32'h0;
            recov_valid_q <= 1'b0;
        end
        else if ((step_valid || ipdom_valid) && !no_recov_c && !ovf_c) begin
            if (push_c)
                frames_q[sp_q[IDXW-1:0]] <= push_frame_c;      // 压栈
            if (pend_wr_c)
                frames_q[pend_idx_c].pending <= 1'b1;          // 切到 fall 路径
            sp_q          <= sp_c;
            mask_q        <= mask_c;
            recov_pc_q    <= recov_pc_c;
            recov_valid_q <= recov_valid_c;
        end
    end

    assign next_pc_out      = np_c;
    assign active_mask      = mask_q;
    assign active_mask_next = mask_c;
    assign stack_empty      = (sp_q == '0);
    assign stack_depth      = 4'(sp_q);

    assign recov_pc         = recov_pc_q;
    assign recov_valid      = recov_valid_q;

    assign err_no_recov     = no_recov_c;
    assign err_simt_ovf     = ovf_c;
    assign err_internal     = internal_c;

    `UNUSED_VAR(diverge_c)

endmodule

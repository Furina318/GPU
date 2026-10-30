`include "defines.vh"

module cp_regs import cp_pkg::*; #(
    parameter logic [31:0] QUEUE_BASE_DEF = `CMD_QUEUE_BASE,
    parameter logic [31:0] QUEUE_SIZE_DEF = 32'd4096,
    parameter logic [31:0] SEM_BASE_DEF   = 32'h0300_0000,
    parameter logic [31:0] FB_BASE_DEF    = `FB_BASE,
    parameter logic [31:0] TIMEOUT_DEF    = 32'h0010_0000
) (
    input  wire         clk              ,
    input  wire         rst              ,

    // 外部(AXI4-Lite 侧)访问 
    input  wire         ext_wr           ,
    input  wire  [11:0] ext_addr         ,
    input  wire  [31:0] ext_wdata        ,
    input  wire         ext_rd           ,   // 读请求
    output logic [31:0] ext_rdata        ,
    input  wire  [11:0] chk_addr         ,   // WRITE_REG 命令的目标地址
    output logic        chk_wr_ok        ,   // 该地址可否被写
    // 内部读口
    input  wire  [11:0] int_addr         ,
    output logic [31:0] int_rdata        ,
    // 内部写口
    input  wire         int_wr           ,
    input  wire  [11:0] int_wr_addr      ,
    input  wire  [31:0] int_wr_wdata     ,
    output logic        cp_reset_evt     ,   // 本拍 host 写了 CP_RESET
    output logic        doorbell_evt     ,   // 本拍 host 写了 DOORBELL

    // CP 状态机给寄存器文件的信息
    input  wire         busy_i           ,   // 队列非空 或 有命令在处理(CP_STATUS.BUSY)
    input  wire         idle_i           ,   // 队列空 && 无在跑命令 && 无错误(CP_STATUS.IDLE)

    // SM / 引擎的现场
    input  wire  [ 1:0] sm_done_i        ,
    input  wire  [ 3:0] sm0_error_i      ,
    input  wire  [ 3:0] sm1_error_i      ,
    input  wire  [31:0] sm0_fault_pc_i   ,
    input  wire  [31:0] sm1_fault_pc_i   ,
    input  wire  [ 2:0] gemm_status_i    ,
    input  wire  [ 7:0] gemm_error_i     ,
    input  wire  [ 2:0] twod_status_i    ,
    input  wire  [ 7:0] twod_error_i     ,
    output logic [ 1:0] sm_err_clr       ,   // {sm1, sm0}
    output logic [ 1:0] eng_err_clr      ,   // {gemm, 2d}

    // 内部端口:配置寄存器(CP 状态机读)
    output logic [31:0] head_o           ,
    output logic [31:0] tail_o           ,
    output logic [31:0] queue_base_o     ,
    output logic [31:0] queue_size_o     ,
    output logic [31:0] sem_base_o       ,
    output logic [31:0] timeout_o        ,
    output logic [ 1:0] kernel_mask_o    ,
    output logic [31:0] img_entry_o [0:1],
    output logic [31:0] img_size_o  [0:1],
    output logic [31:0] img_flags_o [0:1],
    output logic [ 3:0] cp_error_o       ,
    output logic [ 7:0] irq_status_o     ,
    output logic [ 7:0] irq_enable_o     ,
    output logic        doorbell_pend_o  ,

    // 状态机写
    input  wire         head_we          ,
    input  wire [31:0]  head_wdata       ,
    input  wire         tail_we          ,
    input  wire [31:0]  tail_wdata       ,
    input  wire         cp_err_we        ,
    input  wire [3:0]   cp_err_wdata     ,
    input  wire [7:0]   irq_set          ,
    input  wire         doorbell_ack     ,
    input  wire         img_we           ,   // LOAD_KERNEL 完成:记镜像 + 置 KERNEL_MASK
    input  wire [1:0]   img_sm_mask      ,
    input  wire [31:0]  img_entry_w      ,
    input  wire [31:0]  img_size_w       ,
    input  wire [31:0]  img_flags_w      ,

    // 到 host 的中断线
    output logic        irq_o
);
    // 配置与状态寄存器
    logic [31:0] head, tail, queue_base, queue_size, sem_base, fb_base, timeout_cycles;
    logic [1:0]  kernel_mask;
    logic [31:0] img_entry [0:1], img_size [0:1], img_flags [0:1];
    logic [3:0]  cp_error;
    logic [7:0]  irq_status, irq_enable;
    logic        doorbell_pend;

    assign head_o          = head;
    assign tail_o          = tail;
    assign queue_base_o    = queue_base;
    assign queue_size_o    = queue_size;
    assign sem_base_o      = sem_base;
    assign timeout_o       = timeout_cycles;
    assign kernel_mask_o   = kernel_mask;
    assign img_entry_o     = img_entry;
    assign img_size_o      = img_size;
    assign img_flags_o     = img_flags;
    assign cp_error_o      = cp_error;
    assign irq_status_o    = irq_status;
    assign irq_enable_o    = irq_enable;
    assign doorbell_pend_o = doorbell_pend;

    // irq = IRQ_STATUS & IRQ_ENABLE
    assign irq_o = |(irq_status & irq_enable);

    // 哪些偏移可以被 `WRITE_REG` 命令写
    always_comb begin
        case (chk_addr)
            {4'h0, `MMIO_CP_ERROR      }, {4'h0, `MMIO_CMD_TAIL  }, {4'h0, `MMIO_DOORBELL},
            {4'h0, `MMIO_IRQ_STATUS    }, {4'h0, `MMIO_IRQ_ENABLE}, {4'h0, `MMIO_SEM_BASE}, {4'h0, `MMIO_CMD_QUEUE_BASE},
            {4'h0, `MMIO_CMD_QUEUE_SIZE}, {4'h0, `MMIO_FB_BASE   }, {4'h0, `MMIO_CP_RESET}, {4'h0, `MMIO_TIMEOUT_CYCLES},
            {4'h0, `MMIO_SM0_ERROR     }, {4'h0, `MMIO_SM1_ERROR },
            {4'h0, `MMIO_GEMM_ERROR    }, {4'h0, `MMIO_2D_ERROR  }: chk_wr_ok = 1'b1;
            default:                                                chk_wr_ok = 1'b0;
        endcase
    end

    function automatic logic [31:0] read_reg(input logic [11:0] a);
        case (a)
            {4'h0, `MMIO_CP_STATUS}:       return {29'h0, cp_error != CPERR_NONE, busy_i, idle_i};
            {4'h0, `MMIO_CP_ERROR}:        return {28'h0, cp_error};
            {4'h0, `MMIO_CMD_HEAD}:        return head;
            {4'h0, `MMIO_CMD_TAIL}:        return tail;
            {4'h0, `MMIO_DOORBELL}:        return 32'h0;
            {4'h0, `MMIO_IRQ_STATUS}:      return {24'h0, irq_status};
            {4'h0, `MMIO_IRQ_ENABLE}:      return {24'h0, irq_enable};
            {4'h0, `MMIO_SM_DONE}:         return {30'h0, sm_done_i};
            {4'h0, `MMIO_SM0_ERROR}:       return {28'h0, sm0_error_i};
            {4'h0, `MMIO_SM1_ERROR}:       return {28'h0, sm1_error_i};
            {4'h0, `MMIO_SEM_BASE}:        return sem_base;
            {4'h0, `MMIO_CMD_QUEUE_BASE}:  return queue_base;
            {4'h0, `MMIO_CMD_QUEUE_SIZE}:  return queue_size;
            {4'h0, `MMIO_CONST_BASE}:      return `CONST_BASE;
            {4'h0, `MMIO_FB_BASE}:         return fb_base;
            {4'h0, `MMIO_DDR_BASE}:        return `DDR_BASE;
            {4'h0, `MMIO_CP_RESET}:        return 32'h0;
            {4'h0, `MMIO_TIMEOUT_CYCLES}:  return timeout_cycles;
            {4'h0, `MMIO_SM_KERNEL_MASK}:  return {30'h0, kernel_mask};
            {4'h0, `MMIO_SM0_FAULT_PC}:    return sm0_fault_pc_i;
            {4'h0, `MMIO_SM1_FAULT_PC}:    return sm1_fault_pc_i;
            {4'h0, `MMIO_GEMM_STATUS}:     return {29'h0, gemm_status_i};
            {4'h0, `MMIO_GEMM_ERROR}:      return {24'h0, gemm_error_i};
            {4'h0, `MMIO_2D_STATUS}:       return {29'h0, twod_status_i};
            {4'h0, `MMIO_2D_ERROR}:        return {24'h0, twod_error_i};
            default:               return 32'h0;
        endcase
    endfunction

    `UNUSED_VAR(ext_rd)           
    assign ext_rdata = read_reg(ext_addr);
    assign int_rdata = read_reg(int_addr);

    // 外部写的合法性
    wire que_cfg_wr = (ext_addr == {4'h0, `MMIO_CMD_QUEUE_BASE}) || (ext_addr == {4'h0, `MMIO_CMD_QUEUE_SIZE});
    wire ext_wr_en  = ext_wr & (que_cfg_wr ? idle_i : 1'b1);
    wire wr_en      = int_wr | ext_wr_en;               // 内部(命令)优先
    wire [11:0] wr_addr = int_wr ? int_wr_addr : ext_addr;
    wire [31:0] wr_data = int_wr ? int_wr_wdata: ext_wdata;

    always_ff @(posedge clk) begin
        if (rst) begin
            head           <= QUEUE_BASE_DEF;
            tail           <= QUEUE_BASE_DEF;
            queue_base     <= QUEUE_BASE_DEF;
            queue_size     <= QUEUE_SIZE_DEF;
            sem_base       <= SEM_BASE_DEF;
            fb_base        <= FB_BASE_DEF;
            timeout_cycles <= TIMEOUT_DEF;
            kernel_mask    <= 2'b00;
            cp_error       <= CPERR_NONE;
            irq_status     <= 8'h0;
            irq_enable     <= 8'h0;
            doorbell_pend  <= 1'b0;
            for (int i = 0; i < 2; i++) begin
                img_entry[i] <= 32'h0; 
                img_size[i]  <= 32'h0; 
                img_flags[i] <= 32'h0;
            end
        end
        else begin
            // 内部写(状态机)优先级高于外部写
            if (head_we) head <= head_wdata;
            else if (ext_wr_en && ext_addr == {4'h0, `MMIO_CMD_HEAD}) begin
                // 只接受队列范围内的值, 越界丢弃
                if (ext_wdata >= queue_base && ext_wdata < queue_base + queue_size &&
                    ext_wdata[1:0] == 2'b00)
                    head <= ext_wdata;
            end

            if (tail_we) tail <= tail_wdata;

            if (cp_err_we && cp_error == CPERR_NONE)  
                cp_error <= cp_err_wdata;

            irq_status <= (irq_status | irq_set);

            // DOORBELL:置位优先(CP 忙时敲门不能丢)
            if (doorbell_evt)          doorbell_pend <= 1'b1;
            else if (doorbell_ack)     doorbell_pend <= 1'b0;

            if (img_we) begin
                kernel_mask <= kernel_mask | img_sm_mask;
                for (int i = 0; i < 2; i++)
                    if (img_sm_mask[i]) begin
                        img_entry[i] <= img_entry_w;
                        img_size [i] <= img_size_w;
                        img_flags[i] <= img_flags_w;
                    end
            end

            // 外部/内部(命令)写
            if (wr_en) begin
                case (wr_addr)
                    {4'h0, `MMIO_CP_ERROR}:    cp_error    <= CPERR_NONE;          // 写任意值清零
                    {4'h0, `MMIO_IRQ_STATUS}:  irq_status  <= 8'h0;                // 写任意值清零
                    {4'h0, `MMIO_IRQ_ENABLE}:  irq_enable  <= wr_data[7:0];
                    {4'h0, `MMIO_CMD_TAIL}:    tail        <= wr_data;
                    {4'h0, `MMIO_SEM_BASE}:    sem_base    <= wr_data;
                    {4'h0, `MMIO_FB_BASE}:     fb_base     <= wr_data;
                    {4'h0, `MMIO_TIMEOUT_CYCLES}: timeout_cycles <= (wr_data == 32'h0) ? TIMEOUT_DEF
                                                                                       : wr_data;
                    {4'h0, `MMIO_CMD_QUEUE_BASE}: begin
                        if (wr_data >= `MMIO_END && wr_data[1:0] == 2'b00) begin   // 非法值忽略
                            queue_base <= wr_data;
                            head       <= wr_data;
                            tail       <= wr_data;
                        end
                    end
                    {4'h0, `MMIO_CMD_QUEUE_SIZE}: begin
                        if (wr_data >= 32'd8 && wr_data[2:0] == 3'b000) begin       // ≥8 且 8 的倍数
                            queue_size <= wr_data;
                            head       <= queue_base;
                            tail       <= queue_base;
                        end
                    end
                    default: ;
                endcase
            end

            // 清错误/中断/HEAD/镜像
            if (cp_reset_evt) begin
                head          <= tail;
                cp_error      <= CPERR_NONE;
                irq_status    <= 8'h0;
                kernel_mask   <= 2'b00;
                doorbell_pend <= 1'b0;
            end
        end
    end

    assign cp_reset_evt = ext_wr_en & (ext_addr == {4'h0, `MMIO_CP_RESET}) & (ext_wdata[0]);
    assign doorbell_evt = ext_wr_en & (ext_addr == {4'h0, `MMIO_DOORBELL});

    // 清零扇出到拥有者(SM / 引擎)
    assign sm_err_clr  = {ext_wr_en & (ext_addr == {4'h0, `MMIO_SM1_ERROR}),
                          ext_wr_en & (ext_addr == {4'h0, `MMIO_SM0_ERROR})};
    assign eng_err_clr = {ext_wr_en & (ext_addr == {4'h0, `MMIO_GEMM_ERROR}),
                          ext_wr_en & (ext_addr == {4'h0, `MMIO_2D_ERROR})};

endmodule

`include "defines.vh"

// ============================================================================
//  fetch_imem_wrap —— **仿真用**的接线:fetch + 真正的指令 SRAM(mem/instr_mem.sv)
//
//  它不属于设计本体(合成时的接线在 SM 顶层 core.sv 里, 见 docs/MEM_LSU.md),
//  存在的唯一理由是 cocotb 需要一个 toplevel 才能把两块 RTL 一起 elaborate:
//  tb/harness/duts.py 的 `fetch_imem` DUT 用它, 让取指单元面对的是**真实 SRAM**
//  而不是测试台里的假 imem。
//
//  接线要点(与 SPEC.md §3.2 / docs/MEM_LSU.md 一致):
//    · fetch.pc 是字节地址 ⇒ 取指口取 pc[13:2] 当字地址;
//    · fetch 自己已经检查过 PC 的 4B 对齐与 16KB 范围(pc_ok), 越界 PC 不会下到
//      SRAM(它直接报 err_imem 停机), 所以这里切片即可;
//    · 预加载口原样引到顶层, 由测试台扮演 CP 的 LOAD_KERNEL 写入镜像。
// ============================================================================

module fetch_imem_wrap #(
    parameter int NUM_WORDS = `INSTR_SRAM_SIZE / 4
) (
    input  wire                     clk            ,
    input  wire                     rst            ,
    input  wire                     halt           ,
    input  wire                     launch         ,
    input  wire [31:0]              launch_pc      ,
    input  wire [`WARP_SIZE-1:0]    launch_warp    ,
    input  wire                     fet_in_valid   ,
    output wire                     fet_in_ready   ,
    output wire                     fet_out_valid  ,
    input  wire                     fet_out_ready  ,
    output wire [31:0]              pc             ,
    output wire [31:0]              inst           ,
    output wire [`WARP_SIZE-1:0]    inst_mask      ,
    input  wire [31:0]              target_pc      ,
    input  wire                     target_valid   ,
    input  wire [`WARP_SIZE-1:0]    target_taken   ,
    output wire                     br_pending     ,
    output wire                     br_local       ,
    output wire                     br_resolved    ,
    output wire [`WARP_SIZE-1:0]    active_mask    ,
    output wire                     stack_empty    ,
    output wire [3:0]               stack_depth    ,
    output wire [31:0]              fault_pc       ,
    output wire                     err_imem       ,
    output wire                     err_no_recov   ,
    output wire                     err_simt_ovf   ,
    output wire                     err_internal   ,
    output wire                     halted         ,
    // 指令 SRAM 的预加载口(CP 的 LOAD_KERNEL)
    input  wire                     load_en        ,
    input  wire [12:0]              load_addr      ,
    input  wire [31:0]              load_data      ,
    input  wire                     load_last      ,
    output wire                     load_done      ,
    output wire                     err_ovf
);
    localparam int ADDR_W = $clog2(NUM_WORDS);

    wire        imem_on;
    wire [31:0] imem_pc;
    wire        imem_rsp_valid;
    wire [31:0] imem_inst;

    fetch u_fetch (
        .clk            (clk            ),
        .rst            (rst            ),
        .halt           (halt           ),
        .launch         (launch         ),
        .launch_pc      (launch_pc      ),
        .launch_warp    (launch_warp    ),
        .fet_in_valid   (fet_in_valid   ),
        .fet_in_ready   (fet_in_ready   ),
        .fet_out_valid  (fet_out_valid  ),
        .fet_out_ready  (fet_out_ready  ),
        .pc             (pc             ),
        .inst           (inst           ),
        .inst_mask      (inst_mask      ),
        .target_pc      (target_pc      ),
        .target_valid   (target_valid   ),
        .target_taken   (target_taken   ),
        .imem_req_valid (imem_on        ),
        .imem_req_pc    (imem_pc        ),
        .imem_rsp_valid (imem_rsp_valid ),
        .imem_rsp_inst  (imem_inst      ),
        .br_pending     (br_pending     ),
        .br_local       (br_local       ),
        .br_resolved    (br_resolved    ),
        .active_mask    (active_mask    ),
        .stack_empty    (stack_empty    ),
        .stack_depth    (stack_depth    ),
        .fault_pc       (fault_pc       ),
        .err_imem       (err_imem       ),
        .err_no_recov   (err_no_recov   ),
        .err_simt_ovf   (err_simt_ovf   ),
        .err_internal   (err_internal   ),
        .halted         (halted         )
    );

    instr_mem #(
        .NUM_WORDS (NUM_WORDS),
        .ADDR_W    (ADDR_W   )
    ) u_instr_mem (
        .clk        (clk                    ),
        .rst        (rst                    ),
        .load_en    (load_en                ),
        .load_addr  (load_addr              ),
        .load_data  (load_data              ),
        .load_last  (load_last              ),
        .load_done  (load_done              ),
        .err_ovf    (err_ovf                ),
        .fetch_en   (imem_on                ),
        .fetch_addr (imem_pc[ADDR_W+1:2]    ),   // 字节地址 → 字地址(PC 已由 fetch 查过)
        .fetch_data (imem_inst              )
    );

    // SRAM 的读数据在下一拍有效 ⇒ 用"上一拍发过请求"当作 rsp valid。
    // 顺手把请求地址的高位与对齐位用掉: fetch 的 pc_ok 已经保证"4B 对齐 + 16KB 内",
    // 这里一旦看见越界/未对齐的请求就说明它的检查坏了 ⇒ 不回数据(取指单元此时也已停机)。
    wire imem_req_bad = (imem_pc[1:0] != 2'b00) | (|imem_pc[31:ADDR_W+2]);

    reg req_d;
    always_ff @(posedge clk) begin
        if (rst) req_d <= 1'b0;
        else     req_d <= imem_on;
    end
    assign imem_rsp_valid = req_d & ~imem_req_bad;

endmodule

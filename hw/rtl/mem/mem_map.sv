`include "defines.vh"

// ============================================================================
//  mem_map —— 地址区解码(SPEC §2 的唯一地址权威,  ISA D14 / CMDS §8.2)
//
//  把 LSU 送来的字对齐地址翻成"属于哪个区 + 能不能写", 并给出两类架构错误:
//    · `err_mmio`      : 内核碰了 MMIO 窗口(0x0000_0000–0x0000_0FFF) ⇒ MMIO_ACCESS;
//    · `err_no_region` : 不落在任何已知区 ⇒ NO_MEM_REGION(ISA D14: 报错而不是静默).
//
//  纯组合。区域划分见 SPEC §2;信号量区/FB/内核镜像区的大小是可配置的,
//  这里按"区间上界"判断, 由上层用 `cfg_*` 端口覆盖(默认取 `defines.vh` 的宏)。
// ============================================================================

module mem_map #(
    parameter logic [31:0] CONST_END_ADDR = `CONST_END,
    parameter logic [31:0] IMG_END_ADDR   = `KERNEL_IMG_END
) (
    input  wire [31:0] addr         ,
    input  wire        is_store     ,
    output logic [3:0]  region      ,   // 见下方 REG_* 编码
    output logic        writable    ,
    output logic        err_mmio    ,
    output logic        err_no_region
);

    // 区域编码(与参考模型 `region_name()` 的语义一致)
    localparam logic [3:0] REG_NONE  = 4'd0,
                          REG_MMIO  = 4'd1,
                          REG_QUEUE = 4'd2,
                          REG_CONST = 4'd3,
                          REG_IMAGE = 4'd4,
                          REG_SEM   = 4'd5,
                          REG_FB    = 4'd6,
                          REG_DDR   = 4'd7;

    always_comb begin
        region        = REG_NONE;
        writable      = 1'b0;
        err_mmio      = 1'b0;
        err_no_region = 1'b0;

        if (addr < 32'h0000_1000) begin                    // MMIO: 4KB 窗口
            region   = REG_MMIO;
            err_mmio = 1'b1;                               // 内核访问一律报错(ISA D14)
        end else if ((addr >= 32'h0100_0000) && (addr < 32'h0100_1000)) begin
            region   = REG_QUEUE;                          // 命令环
            writable = 1'b1;
        end else if ((addr >= `CONST_BASE) && (addr <= CONST_END_ADDR)) begin
            region   = REG_CONST;                          // 常量区: host/PARAMS 写, 内核只读
            writable = 1'b0;
        end else if ((addr >= `KERNEL_IMG_BASE) && (addr <= IMG_END_ADDR)) begin
            region   = REG_IMAGE;
            writable = 1'b1;
        end else if ((addr >= 32'h0300_0000) && (addr < 32'h0400_0000)) begin
            region   = REG_SEM;                            // 信号量区
            writable = 1'b1;
        end else if ((addr >= `FB_BASE) && (addr < 32'h8000_0000)) begin
            region   = REG_FB;                             // 帧缓冲 + 驱动自分配缓冲
            writable = 1'b1;
        end else if (addr >= `DDR_BASE) begin
            region   = REG_DDR;
            writable = 1'b1;
        end else begin
            err_no_region = 1'b1;
        end
    end

    /* verilator lint_off UNUSEDSIGNAL */
    wire unused_store = is_store;     // 只读区的写错误由 LSU 决定是否报(见 MEM_LSU §5 D7)
    /* verilator lint_on UNUSEDSIGNAL */

endmodule

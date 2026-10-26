`include "defines.vh"

module instr_mem #(
    parameter int SIZE_BYTES = `INSTR_SRAM_SIZE,  
    parameter int NUM_WORDS  = SIZE_BYTES / 4,    
    parameter int ADDR_W     = $clog2(NUM_WORDS)  
) (
    input  wire              clk        ,
    input  wire              rst        ,

    // CP 预加载(LOAD_KERNEL)
    input  wire              load_en    ,
    input  wire [ADDR_W:0]   load_addr  ,
    input  wire [31:0]       load_data  ,
    input  wire              load_last  ,   // 这一拍是最后一个字 ⇒ 下一拍 load_done
    output logic             load_done  ,
    output logic             err_ovf    ,   // 写越界

    // 取指(PC[ADDR_W+1:2])
    input  wire              fetch_en   ,
    input  wire [ADDR_W-1:0] fetch_addr ,
    output logic [31:0]      fetch_data
);
    logic [31:0] sram [0:NUM_WORDS-1];

    // 预加载口
    always_ff @(posedge clk) begin
        if (rst) begin
            load_done <= 1'b0;
            err_ovf   <= 1'b0;
        end
        else begin
            load_done <= 1'b0;                 // 单周期脉冲
            if (load_en) begin
                // 在 load_last 那一拍之前都要保持写, 收到 load_done 后 CP 立刻撤销 en
                if (load_addr[ADDR_W]) err_ovf <= 1'b1;                    // 越界: 不写
                else                   sram[load_addr[ADDR_W-1:0]] <= load_data;
                if (load_last) load_done <= 1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            fetch_data <= 32'h0000_0000;
        end
        else if (fetch_en) begin
            fetch_data <= sram[fetch_addr];
        end
    end

endmodule

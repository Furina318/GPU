`ifndef AXI4_BUS_SV
`define AXI4_BUS_SV

`include "define.vh"

interface axi4_bus
#(
  parameter int ADDR_W = 64,
  parameter int DATA_W = 512,
  parameter int ID_W   = 32
);

  // ---- Write request address channel (AW) ----
  logic                awvalid;
  logic                awready;
  logic [  ADDR_W-1:0] awaddr;
  logic [    ID_W-1:0] awid;
  logic [         7:0] awlen;     // number of transfers - 1
  logic [         2:0] awsize;    // log2 bytes per transfer
  logic [         1:0] awburst;   // 2'b01 = INCR

  // ---- Write data channel (W) ----
  logic                wvalid;
  logic                wready;
  logic [  DATA_W-1:0] wdata;
  logic [DATA_W/8-1:0] wstrb;
  logic                wlast;

  // ---- Write response channel (B) ----
  logic                bvalid;
  logic                bready;
  logic [    ID_W-1:0] bid;
  logic [         1:0] bresp;     // 2'b00 = OKAY

  // ---- Read request address channel (AR) ----
  logic                arvalid;
  logic                arready;
  logic [  ADDR_W-1:0] araddr;
  logic [    ID_W-1:0] arid;
  logic [         7:0] arlen;
  logic [         2:0] arsize;
  logic [         1:0] arburst;

  // ---- Read response channel (R) ----
  logic                rvalid;
  logic                rready;
  logic [  DATA_W-1:0] rdata;
  logic [    ID_W-1:0] rid;
  logic                rlast;
  logic [         1:0] rresp;

  modport master (
    // AW
    output awvalid, awaddr, awid, awlen, awsize, awburst,
    input  awready,
    // W
    output wvalid, wdata, wstrb, wlast,
    input  wready,
    // B
    input  bvalid, bid, bresp,
    output bready,
    // AR
    output arvalid, araddr, arid, arlen, arsize, arburst,
    input  arready,
    // R
    input  rvalid, rdata, rid, rlast, rresp,
    output rready
  );

  modport slave (
    // AW
    input  awvalid, awaddr, awid, awlen, awsize, awburst,
    output awready,
    // W
    input  wvalid, wdata, wstrb, wlast,
    output wready,
    // B
    output bvalid, bid, bresp,
    input  bready,
    // AR
    input  arvalid, araddr, arid, arlen, arsize, arburst,
    output arready,
    // R
    output rvalid, rdata, rid, rlast, rresp,
    input  rready
  );

endinterface : axi_if

`endif // AXI4_BUS_SV
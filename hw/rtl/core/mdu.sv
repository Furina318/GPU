`include "defines.vh"
import core_pkg::*;

module mdu (
    
);
endmodule

module walloc_17bits(
    input  logic [16:0] src_in,
    input  logic [13:0] cin,
    output logic [13:0] cout_group,
    output logic        cout,
    output logic        s
);
    logic [13:0] c;
    logic [ 4:0] first_s;
    csa csa0 (.in (src_in[16:14]), .cout (c[ 4]), .s (first_s[4]));
    csa csa1 (.in (src_in[13:11]), .cout (c[ 3]), .s (first_s[3]));
    csa csa2 (.in (src_in[10:08]), .cout (c[ 2]), .s (first_s[2]));
    csa csa3 (.in (src_in[07:05]), .cout (c[ 1]), .s (first_s[1]));
    csa csa4 (.in (src_in[04:02]), .cout (c[ 0]), .s (first_s[0]));

    logic [3:0] secnod_s;
    csa csa5 (.in (first_s[4:2]             ), .cout (c[ 8]), .s (secnod_s[3]));
    csa csa6 (.in ({first_s[1:0], src_in[1]}), .cout (c[ 7]), .s (secnod_s[2]));
    csa csa7 (.in ({src_in[0], cin[4:3]}    ), .cout (c[ 6]), .s (secnod_s[1]));
    csa csa8 (.in (cin[2:0]                 ), .cout (c[ 5]), .s (secnod_s[0]));

    logic [1:0] thrid_s;
    csa csa9 (.in (secnod_s[3:1]          ), .cout (c[10]), .s (thrid_s[1]));
    csa csaA (.in ({secnod_s[0], cin[6:5]}), .cout (c[ 9]), .s (thrid_s[0]));

    logic [1:0] fourth_s;
    csa csaB (.in ({thrid_s[1:0], cin[10]}), .cout (c[12]), .s (fourth_s[1]));
    csa csaC (.in (cin[9:7]               ), .cout (c[11]), .s (fourth_s[0]));

    logic fifth_s;
    csa csaD (.in ({fourth_s[1:0], cin[11]}), .cout (c[13]), .s (fifth_s));
    csa csaE (.in ({fifth_s, cin[13:12]}   ), .cout (cout ), .s (s      ));

    assign cout_group = c;
endmodule

module csa (
    input  logic [2:0] in  ,
    output logic       cout,
    output logic       s
);
    logic a, b, cin;
    assign {a, b, cin} = in;
    assign cout = (a & b) | (b & cin) | (cin & a);
    assign s    = a ^ b ^ cin;
endmodule

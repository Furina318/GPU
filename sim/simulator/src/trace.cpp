#include "decode.hpp"

std::string itrace(const DecodedInst& d, uint32_t pc) {
    char buf[128];
    if (!d.matched) {
        std::snprintf(buf, sizeof(buf), "illegal 0x%08x", d.raw);
        return buf;
    }
    const uint32_t op = d.raw & 0x7F;
    switch (op) {
    case OP_R:
        std::snprintf(buf, sizeof(buf), "%s r%u, r%u, r%u", d.name, d.rd, d.rs1, d.rs2);
        break;
    case OP_R4:
        std::snprintf(buf, sizeof(buf), "%s r%u, r%u, r%u, r%u",
                      d.name, d.rd, d.rs1, d.rs2, d.rs3);
        break;
    case OP_IMM:
        if (d.funct3 == 0b001 || d.funct3 == 0b101)
            std::snprintf(buf, sizeof(buf), "%s r%u, r%u, %u", d.name, d.rd, d.rs1, d.shamt);
        else
            std::snprintf(buf, sizeof(buf), "%s r%u, r%u, %d", d.name, d.rd, d.rs1, d.imm);
        break;
    case OP_LD:
        std::snprintf(buf, sizeof(buf), "%s r%u, %d(r%u)", d.name, d.rd, d.imm, d.rs1);
        break;
    case OP_ST:
        std::snprintf(buf, sizeof(buf), "%s r%u, %d(r%u)", d.name, d.rs2, d.imm, d.rs1);
        break;
    case OP_LUI:
        std::snprintf(buf, sizeof(buf), "lui r%u, 0x%x  ; → 0x%08x",
                      d.rd, unsigned(d.imm_raw) & 0xFFFFFu, unsigned(d.imm));
        break;
    case OP_IPDOM:
        std::snprintf(buf, sizeof(buf), "ipdom 0x%04x", d.target(pc));
        break;
    case OP_BR:
        std::snprintf(buf, sizeof(buf), "br.simt 0x%04x", d.target(pc));
        break;
    case OP_SETP:
        std::snprintf(buf, sizeof(buf), "%s p%u, r%u, r%u", d.name, d.pdest, d.rs1, d.rs2);
        break;
    case OP_CTRL:
        if (d.funct3 == 0b011)      std::snprintf(buf, sizeof(buf), "tmov r%u, tid", d.rd);
        else if (d.funct3 == 0b100) std::snprintf(buf, sizeof(buf), "wmov r%u, wid", d.rd);
        else                        std::snprintf(buf, sizeof(buf), "%s", d.name);
        break;
    default:
        std::snprintf(buf, sizeof(buf), "%s ?", d.name);
        break;
    }
    std::string s;
    if (d.pred.used && (d.pred.psel || d.pred.inv))
        s = (d.pred.inv ? "@!p" : "@p") + std::to_string(d.pred.psel) + " ";
    s += buf;
    if (!d.ok && d.error) s += std::string("   ; ") + d.error;
    return s;
}

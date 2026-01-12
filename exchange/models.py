from dataclasses import dataclass

@dataclass(frozen=True)
class InstrumentSpec:
    inst_id: str
    ct_val: float
    lot_sz: float
    min_sz: float
    tick_sz: float

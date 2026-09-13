"""
Avalanche address model with X/P/C format conversion.

Address formats:
- C-Chain: 0x... (20-byte hex, Ethereum compatible)
- X-Chain: X-avax1... (Bech32 encoded)
- P-Chain: P-avax1... (Bech32 encoded, same underlying address as X)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Self


class Chain(Enum):
    """Avalanche chain identifiers."""
    C = "C"  # Contract chain (EVM compatible)
    X = "X"  # Exchange chain (UTXO, DAG)
    P = "P"  # Platform chain (staking, subnets)


# Bech32 character set
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    """Internal function that computes the Bech32 checksum."""
    generator = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    """Expand the HRP into values for checksum computation."""
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> bool:
    """Verify a checksum given HRP and converted data characters."""
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    """Compute the checksum values given HRP and data."""
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes | list[int], frombits: int, tobits: int, pad: bool = True) -> list[int] | None:
    """General power-of-2 base conversion."""
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def bech32_decode(bech: str) -> tuple[str | None, bytes | None]:
    """Decode a Bech32 string."""
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return (None, None)
    if bech.lower() != bech and bech.upper() != bech:
        return (None, None)
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return (None, None)
    if not all(x in BECH32_CHARSET for x in bech[pos + 1:]):
        return (None, None)
    hrp = bech[:pos]
    data = [BECH32_CHARSET.find(x) for x in bech[pos + 1:]]
    if not _bech32_verify_checksum(hrp, data):
        return (None, None)
    decoded = _convertbits(data[:-6], 5, 8, False)
    if decoded is None:
        return (None, None)
    return (hrp, bytes(decoded))


def bech32_encode(hrp: str, data: bytes) -> str:
    """Encode bytes to Bech32 string."""
    converted = _convertbits(data, 8, 5)
    if converted is None:
        raise ValueError("Invalid data for Bech32 encoding")
    checksum = _bech32_create_checksum(hrp, converted)
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in converted + checksum)


@dataclass(frozen=True)
class AvaxAddress:
    """
    Avalanche address with support for all three chain formats.

    The underlying address is stored as 20 bytes. X and P chain addresses
    with the same suffix (after avax1) represent the same key.

    Examples:
        >>> addr = AvaxAddress.from_c_address("0x6f9bcc68976650daffece88546f3be45f252e1eb")
        >>> addr.x_address
        'X-avax1d7duc6yhvegd4llvazz5dua7ghe99c0tfqu5tr'
        >>> addr.p_address
        'P-avax1d7duc6yhvegd4llvazz5dua7ghe99c0tfqu5tr'
    """
    raw_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.raw_bytes) != 20:
            raise ValueError(f"Address must be 20 bytes, got {len(self.raw_bytes)}")

    @classmethod
    def from_c_address(cls, address: str) -> Self:
        """Create from C-chain (0x...) address."""
        if address.startswith("0x"):
            address = address[2:]
        if len(address) != 40:
            raise ValueError(f"Invalid C-chain address length: {len(address)}")
        return cls(bytes.fromhex(address))

    @classmethod
    def from_x_address(cls, address: str) -> Self:
        """Create from X-chain (X-avax1...) address."""
        if address.startswith("X-"):
            address = address[2:]
        hrp, data = bech32_decode(address)
        if hrp != "avax" or data is None:
            raise ValueError(f"Invalid X-chain address: {address}")
        return cls(data)

    @classmethod
    def from_p_address(cls, address: str) -> Self:
        """Create from P-chain (P-avax1...) address."""
        if address.startswith("P-"):
            address = address[2:]
        hrp, data = bech32_decode(address)
        if hrp != "avax" or data is None:
            raise ValueError(f"Invalid P-chain address: {address}")
        return cls(data)

    @classmethod
    def from_any(cls, address: str) -> Self:
        """Parse address in any format (auto-detect)."""
        address = address.strip()
        if address.startswith("0x"):
            return cls.from_c_address(address)
        elif address.startswith("X-"):
            return cls.from_x_address(address)
        elif address.startswith("P-"):
            return cls.from_p_address(address)
        elif address.startswith("avax1"):
            # Bech32 without chain prefix, assume X-chain
            hrp, data = bech32_decode(address)
            if hrp != "avax" or data is None:
                raise ValueError(f"Invalid Avalanche address: {address}")
            return cls(data)
        else:
            # Try as hex without 0x prefix
            try:
                if len(address) == 40:
                    return cls(bytes.fromhex(address))
            except ValueError:
                pass
            raise ValueError(f"Unknown address format: {address}")

    @property
    def c_address(self) -> str:
        """Get C-chain format (0x...)."""
        return "0x" + self.raw_bytes.hex()

    @property
    def x_address(self) -> str:
        """Get X-chain format (X-avax1...)."""
        return "X-" + bech32_encode("avax", self.raw_bytes)

    @property
    def p_address(self) -> str:
        """Get P-chain format (P-avax1...)."""
        return "P-" + bech32_encode("avax", self.raw_bytes)

    def for_chain(self, chain: Chain) -> str:
        """Get address in the format for a specific chain."""
        if chain == Chain.C:
            return self.c_address
        elif chain == Chain.X:
            return self.x_address
        elif chain == Chain.P:
            return self.p_address
        raise ValueError(f"Unknown chain: {chain}")

    def __str__(self) -> str:
        """Default string representation uses C-chain format."""
        return self.c_address

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AvaxAddress):
            return self.raw_bytes == other.raw_bytes
        return False

    def __hash__(self) -> int:
        return hash(self.raw_bytes)

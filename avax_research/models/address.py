"""
Avalanche address model.

Address formats:
- C-Chain: 0x... (Ethereum address: keccak256(public key)[-20:])
- X-Chain: X-avax1... (bech32 of ripemd160(sha256(compressed public key)))
- P-Chain: P-avax1... (the same 20 bytes as the X-Chain address)

P and X addresses with the same bech32 suffix are the same owner. A C-Chain address is a different
hash of the key, so it cannot be converted to or from a P/X address: re-encoding the 20 bytes names
an unrelated address. `AvaxAddress` therefore records which family it belongs to, and only offers the
formats that are real for it. The two are linked by a public key (signer credentials in X-Chain and
C-Chain transactions), not by their bytes.
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


class AddressKind(Enum):
    """Which address family a 20-byte address belongs to."""
    EVM = "evm"  # C-Chain
    PRIMARY = "primary"  # P-Chain and X-Chain


@dataclass(frozen=True)
class AvaxAddress:
    """
    An Avalanche address: 20 bytes plus the family they belong to.

    Examples:
        >>> addr = AvaxAddress.from_any("P-avax1d7duc6yhvegd4llvazz5dua7ghe99c0tfqu5tr")
        >>> addr.x_address
        'X-avax1d7duc6yhvegd4llvazz5dua7ghe99c0tfqu5tr'
        >>> addr.c_address  # no C-Chain form: that would be a different address
        ''
    """
    raw_bytes: bytes = field(repr=False)
    kind: AddressKind

    def __post_init__(self) -> None:
        if len(self.raw_bytes) != 20:
            raise ValueError(f"Address must be 20 bytes, got {len(self.raw_bytes)}")

    @classmethod
    def from_c_address(cls, address: str) -> Self:
        """Create from a C-Chain (0x...) address."""
        if address.startswith("0x"):
            address = address[2:]
        if len(address) != 40:
            raise ValueError(f"Invalid C-chain address length: {len(address)}")
        return cls(bytes.fromhex(address), AddressKind.EVM)

    @classmethod
    def from_x_address(cls, address: str) -> Self:
        """Create from an X-Chain (X-avax1...) or bare (avax1...) address."""
        if address.startswith("X-"):
            address = address[2:]
        hrp, data = bech32_decode(address)
        if hrp != "avax" or data is None:
            raise ValueError(f"Invalid X-chain address: {address}")
        return cls(data, AddressKind.PRIMARY)

    @classmethod
    def from_p_address(cls, address: str) -> Self:
        """Create from a P-Chain (P-avax1...) or bare (avax1...) address."""
        if address.startswith("P-"):
            address = address[2:]
        hrp, data = bech32_decode(address)
        if hrp != "avax" or data is None:
            raise ValueError(f"Invalid P-chain address: {address}")
        return cls(data, AddressKind.PRIMARY)

    @classmethod
    def from_any(cls, address: str) -> Self:
        """Parse an address in any format; the format decides the family."""
        address = address.strip()
        if address.startswith("0x"):
            return cls.from_c_address(address)
        if address.startswith("X-"):
            return cls.from_x_address(address)
        if address.startswith("P-"):
            return cls.from_p_address(address)
        if address.startswith("avax1"):
            return cls.from_x_address(address)
        if len(address) == 40:
            try:
                return cls(bytes.fromhex(address), AddressKind.EVM)
            except ValueError:
                pass
        raise ValueError(f"Unknown address format: {address}")

    @property
    def is_evm(self) -> bool:
        return self.kind is AddressKind.EVM

    @property
    def is_primary(self) -> bool:
        return self.kind is AddressKind.PRIMARY

    @property
    def c_address(self) -> str:
        """C-Chain form (0x...), or "" for a P/X address."""
        return "0x" + self.raw_bytes.hex() if self.is_evm else ""

    @property
    def bech32(self) -> str:
        """Bare bech32 (avax1...), as the Data API returns P/X addresses; "" for a C-Chain address."""
        return bech32_encode("avax", self.raw_bytes) if self.is_primary else ""

    @property
    def x_address(self) -> str:
        """X-Chain form (X-avax1...), or "" for a C-Chain address."""
        return "X-" + self.bech32 if self.is_primary else ""

    @property
    def p_address(self) -> str:
        """P-Chain form (P-avax1...), or "" for a C-Chain address."""
        return "P-" + self.bech32 if self.is_primary else ""

    @property
    def match_strings(self) -> set[str]:
        """Lowercase forms this address can appear as in API responses."""
        if self.is_evm:
            return {self.c_address.lower()}
        return {self.bech32, self.x_address.lower(), self.p_address.lower()}

    def for_chain(self, chain: Chain) -> str:
        """The address in a chain's format. Raises ValueError across families (C versus P/X)."""
        value = {Chain.C: self.c_address, Chain.X: self.x_address, Chain.P: self.p_address}[chain]
        if not value:
            raise ValueError(f"{self} has no {chain.value}-Chain form")
        return value

    def __str__(self) -> str:
        """0x... for C-Chain addresses, avax1... for P/X addresses."""
        return self.c_address or self.bech32

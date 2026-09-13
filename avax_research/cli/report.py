"""
CLI for viewing from-genesis trace results.

Usage:
    python -m avax_research.cli.report
    python -m avax_research.cli.report results/20260125_121252_from_genesis.csv
    python -m avax_research.cli.report --top 50
"""

import argparse
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

# Increase CSV field size limit for large import_tx_hashes fields
csv.field_size_limit(sys.maxsize)


def find_latest_csv(directory: str = "data/results") -> Path | None:
    """Find the most recent from_genesis.csv file."""
    results_dir = Path(directory)
    if not results_dir.exists():
        return None

    csv_files = list(results_dir.glob("*_from_genesis.csv"))
    if not csv_files:
        return None

    return max(csv_files, key=lambda p: p.stat().st_mtime)


def load_data(csv_path: Path) -> list[dict]:
    """Load and parse CSV data."""
    data = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append(row)
    return data


def print_report(data: list[dict], csv_path: Path, top_n: int = 30) -> None:
    """Print formatted report."""
    if not data:
        print("No data found.")
        return

    # Sort by amount
    data.sort(key=lambda x: float(x['total_avax']), reverse=True)

    print("\n" + "=" * 110)
    print("                              TOP C-CHAIN DESTINATIONS FROM GENESIS")
    print("=" * 110 + "\n")

    print(f"{'C-Chain Address':<44} {'AVAX':>15}  {'Depth':>5}  {'Funder Category':<40}")
    print("-" * 44 + " " + "-" * 15 + "  " + "-" * 5 + "  " + "-" * 40)

    for row in data[:top_n]:
        addr = row['c_address']
        avax = float(row['total_avax'])
        depth = row.get('max_depth', row.get('min_depth', '?'))
        cat = row['categories'][:40]
        print(f"{addr:<44} {avax:>15,.0f}  {depth:>5}  {cat:<40}")

    print("\n" + "=" * 110)
    print("                                         SUMMARY")
    print("=" * 110 + "\n")

    total_avax = sum(float(r['total_avax']) for r in data)
    print(f"  Total C-chain destinations: {len(data):,}")
    print(f"  Total AVAX exported to C-chain: {total_avax:,.0f}")

    # Depth stats
    try:
        depths = [int(r['max_depth']) for r in data]
        print(f"  Max depth reached: {max(depths)}")

        # Depth distribution
        depth_counts = defaultdict(int)
        for d in depths:
            depth_counts[d] += 1
        print(f"  By depth: {dict(sorted(depth_counts.items()))}")
    except (KeyError, ValueError):
        pass

    print("\n  By Genesis Category:")
    by_cat = defaultdict(lambda: {'count': 0, 'avax': 0})
    for row in data:
        cat = row['categories']
        by_cat[cat]['count'] += 1
        by_cat[cat]['avax'] += float(row['total_avax'])

    for cat, stats in sorted(by_cat.items(), key=lambda x: -x[1]['avax']):
        print(f"    {cat:<50} {stats['count']:>6} wallets  {stats['avax']:>15,.0f} AVAX")

    print(f"\n  File: {csv_path}")
    mtime = csv_path.stat().st_mtime
    print(f"  Last updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))}")

    # Check if still being written
    age_seconds = time.time() - mtime
    if age_seconds < 60:
        print(f"  Status: ACTIVE (updated {age_seconds:.0f}s ago)")
    elif age_seconds < 300:
        print(f"  Status: Recently updated ({age_seconds/60:.1f} min ago)")
    else:
        print(f"  Status: Idle ({age_seconds/60:.0f} min since last update)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="View from-genesis trace results",
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        help="Path to CSV file (default: most recent in results/)",
    )
    parser.add_argument(
        "--top", "-n",
        type=int,
        default=30,
        help="Number of top destinations to show (default: 30)",
    )
    parser.add_argument(
        "--dir", "-d",
        default="data/results",
        help="Directory to search for CSV files (default: results)",
    )

    args = parser.parse_args()

    # Find CSV file
    if args.csv_file:
        csv_path = Path(args.csv_file)
    else:
        csv_path = find_latest_csv(args.dir)

    if not csv_path or not csv_path.exists():
        print(f"No from_genesis.csv files found in {args.dir}/")
        return 1

    # Load and display
    data = load_data(csv_path)
    print_report(data, csv_path, args.top)

    return 0


if __name__ == "__main__":
    sys.exit(main())

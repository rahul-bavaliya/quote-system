"""Step 1 - read the raw exports (xlsx with any number of sheets, or csv) into clean Parquet.

The exports from the BI tool have a couple of junk rows above the header and Power-BI style
column names such as ``ServiceOrder[Wo Number]``. We find the header row automatically and map
columns by alias, so a plain CSV with simple headers (``Make``, ``Model``, ``Qty`` ...) works too.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import duckdb
import polars as pl

from .config import Settings

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# canonical column -> accepted header aliases (compared after _norm)
WO_COLUMNS: dict[str, list[str]] = {
    "service_order_id": ["ServiceOrder[ServiceOrderID]", "ServiceOrderID", "Service Order ID", "SO ID"],
    "wo_number": ["ServiceOrder[Wo Number]", "Wo Number", "WO Number", "Work Order", "WO"],
    "wo_segment": ["ServiceOrder[Wo Segment]", "Wo Segment", "WO Segment", "Segment"],
    "country": ["Branch[Branch Country]", "Branch Country", "Country"],
    "branch": ["Branch[Branch Name]", "Branch Name", "Branch"],
    "division": ["ServiceOrder[Contract Type]", "Contract Type", "Division"],
    "location": ["ServiceOrder[ShopOffsite Description]", "ShopOffsite Description", "Shop/Offsite", "Location"],
    "make": ["ServiceOrder[Make]", "Make"],
    "model": ["ServiceOrder[Model]", "Model"],
    "meter": ["ServiceOrder[Meter]", "Meter", "Machine Hours", "Hours Meter"],
    "description": ["ServiceOrder[Description]", "Description", "Segment Description", "WO Description"],
    "sales_type": ["ServiceOrder Sales Type[Description]", "Sales Type"],
    "status": ["ServiceOrder Status[Description]", "Status"],
    "open_date": ["ServiceOrder[Open Date]", "Open Date"],
    "invoice_date": ["ServiceOrder[Invoice Date]", "Invoice Date"],
    "eq_hours": ["ServiceOrder[EQ Labor Hours]", "EQ Labor Hours", "EQ Hours", "Estimated Hours", "Quoted Hours"],
    "actual_hours": ["[M_Actual_ServiceOrderLaborHrsTotal]", "M_Actual_ServiceOrderLaborHrsTotal",
                     "Actual Labor Hours", "Actual Hours", "Labor Hours"],
}
WO_REQUIRED = ["service_order_id", "make", "model", "description", "actual_hours"]

PART_COLUMNS: dict[str, list[str]] = {
    "service_order_id": ["ServiceOrder Product Detail[ServiceOrderID]", "ServiceOrderID", "Service Order ID"],
    "part_number": ["ServiceOrder Product Detail[Product Number]", "Product Number", "Part Number", "Part No", "PartNumber"],
    "part_description": ["ServiceOrder Product Detail[Description]", "Description", "Part Description"],
    "qty": ["ServiceOrder Product Detail[ServiceOrderPartsShipQty]", "ServiceOrderPartsShipQty", "Qty", "Quantity", "Ship Qty"],
}
PART_REQUIRED = ["service_order_id", "part_number", "qty"]


def _match_columns(headers: list, spec: dict[str, list[str]]) -> dict[str, int]:
    normed = [_norm(h) if h is not None else "" for h in headers]
    found: dict[str, int] = {}
    for canon, aliases in spec.items():
        for alias in aliases:  # aliases are ordered most-specific first
            a = _norm(alias)
            if a in normed and normed.index(a) not in found.values():
                found[canon] = normed.index(a)
                break
    return found


def _read_frames(path: Path) -> list[tuple[str, pl.DataFrame]]:
    """Return every sheet (or the csv) as an all-string frame with generic column names, no header."""
    if path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        import fastexcel

        reader = fastexcel.read_excel(str(path))
        out = []
        for name in reader.sheet_names:
            df = reader.load_sheet(name, header_row=None, dtypes="string").to_polars()
            out.append((name, df))
        return out
    if path.suffix.lower() in {".csv", ".txt"}:
        df = pl.read_csv(path, has_header=False, infer_schema=False, truncate_ragged_lines=True,
                         encoding="utf8-lossy")
        return [(path.stem, df)]
    raise ValueError(f"Unsupported file type: {path}")


def _extract(df: pl.DataFrame, spec, required, label: str) -> pl.DataFrame | None:
    best = None
    for i in range(min(15, df.height)):
        row = df.row(i)
        m = _match_columns(list(row), spec)
        if all(r in m for r in required):
            best = (i, m)
            break
    if best is None:
        log.warning("%s: no header row with columns %s found - skipped", label, required)
        return None
    hdr, mapping = best
    body = df.slice(hdr + 1)
    cols = body.columns
    out = pl.DataFrame({canon: body[cols[idx]] for canon, idx in mapping.items()})
    for canon in spec:
        if canon not in out.columns:
            out = out.with_columns(pl.lit(None, dtype=pl.String).alias(canon))
    log.info("%s: header on row %d, %d data rows", label, hdr + 1, out.height)
    return out.select(list(spec))


def _find(raw_dir: Path, stem: str) -> list[Path]:
    files = sorted(p for p in raw_dir.iterdir()
                   if p.is_file() and p.name.lower().startswith(stem)
                   and p.suffix.lower() in {".xlsx", ".xlsm", ".xls", ".csv", ".txt"})
    if not files:
        raise FileNotFoundError(
            f"No '{stem}*.xlsx' or '{stem}*.csv' found in {raw_dir}. Put your exports there (see README).")
    return files


def ingest(settings: Settings) -> dict[str, int]:
    settings.ensure_dirs()
    frames = {"workorders": [], "parts": []}
    for kind, spec, req in (("workorders", WO_COLUMNS, WO_REQUIRED), ("parts", PART_COLUMNS, PART_REQUIRED)):
        stem = "workorder" if kind == "workorders" else "part"
        for path in _find(settings.raw_dir, stem):
            for sheet, df in _read_frames(path):
                got = _extract(df, spec, req, f"{path.name}[{sheet}]")
                if got is not None:
                    frames[kind].append(got)
        if not frames[kind]:
            raise ValueError(f"No usable {kind} data found in {settings.raw_dir}")

    con = duckdb.connect()
    wo = pl.concat(frames["workorders"], how="vertical")
    parts = pl.concat(frames["parts"], how="vertical")
    con.register("wo_raw", wo.to_arrow())
    con.register("parts_raw", parts.to_arrow())

    wo_path = settings.work_dir / "workorders.parquet"
    parts_path = settings.work_dir / "parts.parquet"
    ts = "try_cast(try_cast({c} as timestamp) as date)"
    con.execute(f"""
        copy (
          select
            trim(service_order_id)                         as service_order_id,
            try_cast(wo_number as bigint)                  as wo_number,
            try_cast(wo_segment as integer)                as wo_segment,
            nullif(trim(country), '')                      as country,
            nullif(trim(branch), '')                       as branch,
            coalesce(nullif(trim(division), ''), 'UNKNOWN') as division,
            nullif(trim(location), '')                     as location,
            coalesce(nullif(upper(trim(make)), ''), 'UNKNOWN')   as make,
            coalesce(nullif(upper(trim(model)), ''), 'UNKNOWN')  as model,
            try_cast(meter as double)                      as meter,
            coalesce(trim(description), '')                as description,
            nullif(trim(sales_type), '')                   as sales_type,
            nullif(trim(status), '')                       as status,
            {ts.format(c='open_date')}                     as open_date,
            {ts.format(c='invoice_date')}                  as invoice_date,
            try_cast(eq_hours as double)                   as eq_hours,
            try_cast(actual_hours as double)               as actual_hours
          from wo_raw
          where nullif(trim(service_order_id), '') is not null
          qualify row_number() over (partition by trim(service_order_id) order by invoice_date desc nulls last) = 1
        ) to '{wo_path}' (format parquet)
    """)
    # Part descriptions come out of the ERP as a fixed-width string:
    #   <part number padded to 16><8 char description><optional 2 char class code>
    # e.g. 'TY25879B        WET CHARCY'. We strip the part number and the trailing code.
    con.execute(f"""
        copy (
          select service_order_id, part_number,
                 upper(strip_accents(trim(case when part_number <> '' and starts_with(trim(substr(d, 1, 24)), part_number)
                            then substr(trim(substr(d, 1, 24)), length(part_number) + 1)
                            when length(d) >= 17 and part_number <> '' then substr(d, 17, 8)
                            else d end))) as part_description,
                 nullif(trim(substr(d, 25)), '') as part_class,
                 qty
          from (
            select trim(service_order_id) as service_order_id,
                   upper(trim(coalesce(part_number, ''))) as part_number,
                   coalesce(part_description, '') as d,
                   try_cast(qty as double) as qty
            from parts_raw
            where nullif(trim(service_order_id), '') is not null
          )
          where part_number <> ''
        ) to '{parts_path}' (format parquet)
    """)
    counts = {
        "workorder_segments": con.execute(f"select count(*) from '{wo_path}'").fetchone()[0],
        "part_lines": con.execute(f"select count(*) from '{parts_path}'").fetchone()[0],
    }
    log.info("ingest: %s", counts)
    return counts

"""make_mock_data.py - OPTIONAL. Creates fake sample files to try the app with.

Run:   python make_mock_data.py
Then upload the files from the sample_data/ folder in the app.

The app never imports or depends on this script. You can delete it, and the
sample_data/ folder, at any time.

Everything is invented: made-up companies, made-up people, e-mails on
example.com and phone numbers in the 555-01xx range. No real client data.

The files are deliberately a little messy, like real exports:
  clients.xlsx      2 sheets (Clients, Account Managers). A few empty e-mails,
                    inconsistent region spelling, one duplicated row.
  engagements.csv   Money as text ("$12,345.00"), percentages as text ("23.5%"),
                    some empty values, 2 rows pointing to a client that does not
                    exist, 2 duplicated rows.
  invoices.xlsx     Title rows above the real header row, dates, Yes/No column.
Client ID links all three files, so you can ask questions across them.
"""

from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

REFERENCE_DATE = pd.Timestamp("2025-10-01")  # fixed, so the data is the same every run

FIRST_NAMES = ["Aarav", "Maya", "Liam", "Sofia", "Noah", "Priya", "Ethan", "Zara", "Lucas", "Anika",
               "Oliver", "Meera", "Daniel", "Isha", "Mateo", "Leila", "Samuel", "Nina", "Arjun", "Emma"]
LAST_NAMES = ["Shah", "Rao", "Nguyen", "Garcia", "Kim", "Patel", "Brown", "Silva", "Khan", "Meyer",
              "Rossi", "Ito", "Costa", "Novak", "Ali", "Berg", "Reddy", "Lopez", "Dubois", "Nair"]
NAME_PREFIXES = ["Alder", "Birch", "Cobalt", "Delta", "Ember", "Falcon", "Granite", "Harbor",
                 "Iris", "Juniper", "Kestrel", "Lumen"]
NAME_SUFFIXES = ["Logistics", "Foods", "Systems", "Retail", "Health", "Energy", "Labs", "Capital"]
INDUSTRIES = ["Manufacturing", "Retail", "Healthcare", "Financial Services", "Energy", "Technology", "Logistics"]
REGIONS = ["North", "South", "East", "West"]
SERVICE_RATES = {  # service line -> typical hourly rate
    "Strategy": 180, "Data & Analytics": 150, "Technology Delivery": 130,
    "Change Management": 110, "Operations": 120,
}
STATUS_WEIGHTS = {"Completed": 0.55, "In progress": 0.30, "On hold": 0.08, "Cancelled": 0.07}


def make_managers(rng: np.random.Generator) -> pd.DataFrame:
    names = [f"{f} {l}" for f, l in zip(rng.choice(FIRST_NAMES, 8, replace=False),
                                        rng.choice(LAST_NAMES, 8, replace=False))]
    regions = (REGIONS * 2)[:8]
    joined = pd.to_datetime("2018-01-01") + pd.to_timedelta(rng.integers(0, 2000, 8), unit="D")
    return pd.DataFrame({
        "Manager": names,
        "Team": ["Enterprise" if i % 2 == 0 else "Mid-market" for i in range(8)],
        "Region": regions,
        "Joined": joined.normalize(),
    })


def make_clients(rng: np.random.Generator, count: int, managers: pd.DataFrame) -> pd.DataFrame:
    combos = [f"{p} {s}" for p in NAME_PREFIXES for s in NAME_SUFFIXES]
    names = rng.choice(combos, size=count, replace=False)
    regions = rng.choice(REGIONS, count)
    rows = []
    for number, (name, region) in enumerate(zip(names, regions), start=1):
        contact_first, contact_last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        domain = name.lower().replace(" ", "")
        manager = rng.choice(managers.loc[managers["Region"] == region, "Manager"])
        rows.append({
            "Client ID": f"C{number:04d}",
            "Client Name": name,
            "Industry": rng.choice(INDUSTRIES),
            "Region": region,
            "Account Manager": manager,
            "Contact Name": f"{contact_first} {contact_last}",
            "Contact Email": f"{contact_first.lower()}.{contact_last.lower()}@{domain}.example.com",
            "Phone": f"555-01{rng.integers(0, 100):02d}",
            "Onboarded": pd.Timestamp("2021-01-01") + pd.Timedelta(days=int(rng.integers(0, 1300))),
        })
    df = pd.DataFrame(rows)

    # Realistic mess
    df.loc[rng.choice(count, 3, replace=False), "Contact Email"] = np.nan
    df.loc[1, "Region"] = str(df.loc[1, "Region"]).lower() + " "  # e.g. "north "
    df.loc[2, "Region"] = str(df.loc[2, "Region"]).upper()
    df = pd.concat([df, df.iloc[[4]]], ignore_index=True)  # one duplicated client row
    return df


def make_engagements(rng: np.random.Generator, client_ids: list[str], count: int) -> pd.DataFrame:
    weights = rng.dirichlet(np.ones(len(client_ids)) * 0.6)  # some clients buy much more than others
    services = list(SERVICE_RATES)
    rows = []
    for number in range(1, count + 1):
        service = rng.choice(services)
        start = pd.Timestamp("2024-01-01") + pd.Timedelta(days=int(rng.integers(0, 640)))
        end = start + pd.Timedelta(days=int(rng.integers(30, 300)))
        status = rng.choice(list(STATUS_WEIGHTS), p=list(STATUS_WEIGHTS.values()))
        if end > REFERENCE_DATE and status == "Completed":
            status = "In progress"
        hours = float(rng.integers(40, 1200))
        rate = SERVICE_RATES[service] * rng.uniform(0.85, 1.2)
        rows.append({
            "Engagement ID": f"E-{number:05d}",
            "Client ID": rng.choice(client_ids, p=weights),
            "Service Line": service,
            "Start Date": start,
            "End Date": end if status in ("Completed", "Cancelled") else pd.NaT,
            "Status": status,
            "Hours": hours,
            "Spend": round(hours * rate, 2),
            "Margin": round(float(rng.uniform(8, 45)), 1),
        })
    df = pd.DataFrame(rows)

    # Realistic mess
    df.loc[rng.choice(count, 4, replace=False), "Hours"] = np.nan
    df.loc[rng.choice(count, 3, replace=False), "Spend"] = np.nan
    df.loc[[0, 1], "Client ID"] = "C9999"  # a client that is not in clients.xlsx
    df = pd.concat([df, df.iloc[[10, 11]]], ignore_index=True)  # two duplicated rows
    return df


def make_invoices(rng: np.random.Generator, engagements: pd.DataFrame) -> pd.DataFrame:
    rows = []
    unique = engagements.drop_duplicates("Engagement ID")
    for _, engagement in unique.iterrows():
        if engagement["Status"] == "Cancelled" or pd.isna(engagement["Spend"]):
            continue
        parts = int(rng.integers(1, 4))
        for part in range(parts):
            date = engagement["Start Date"] + pd.Timedelta(days=int(30 * (part + 1) + rng.integers(0, 20)))
            if date > REFERENCE_DATE:
                continue
            old = (REFERENCE_DATE - date).days > 90
            paid = rng.random() < (0.85 if old else 0.4)
            paid_date = date + pd.Timedelta(days=int(rng.integers(15, 76))) if paid else pd.NaT
            if paid and paid_date > REFERENCE_DATE:
                paid, paid_date = False, pd.NaT
            rows.append({
                "Engagement ID": engagement["Engagement ID"],
                "Client ID": engagement["Client ID"],
                "Invoice Date": date,
                "Amount": round(float(engagement["Spend"]) / parts, 2),
                "Paid": "Yes" if paid else "No",
                "Paid Date": paid_date,
            })
    df = pd.DataFrame(rows).sort_values("Invoice Date").reset_index(drop=True)
    df.insert(0, "Invoice No", [f"INV-2025-{n:04d}" for n in range(1, len(df) + 1)])
    df["Days to Pay"] = (df["Paid Date"] - df["Invoice Date"]).dt.days
    return df


def _widen(worksheet, df: pd.DataFrame) -> None:
    from openpyxl.utils import get_column_letter

    for position, column in enumerate(df.columns, start=1):
        longest = max([len(str(column))] + [len(str(v)) for v in df[column].head(200)])
        worksheet.column_dimensions[get_column_letter(position)].width = min(40, longest + 2)


def write_files(out_dir: str, clients: int, seed: int) -> list[tuple[str, str, int]]:
    rng = np.random.default_rng(seed)
    os.makedirs(out_dir, exist_ok=True)

    managers = make_managers(rng)
    client_table = make_clients(rng, clients, managers)
    client_ids = list(client_table["Client ID"].unique())
    engagements = make_engagements(rng, client_ids, count=clients * 4)
    invoices = make_invoices(rng, engagements)

    # engagements.csv: money and percentages as TEXT, like a typical export
    csv_table = engagements.copy()
    csv_table["Total Spend"] = csv_table.pop("Spend").map(lambda v: "" if pd.isna(v) else f"${v:,.2f}")
    csv_table["Margin %"] = csv_table.pop("Margin").map(lambda v: f"{v:.1f}%")
    csv_table["Start Date"] = csv_table["Start Date"].dt.strftime("%Y-%m-%d")
    csv_table["End Date"] = csv_table["End Date"].dt.strftime("%Y-%m-%d")
    csv_path = os.path.join(out_dir, "engagements.csv")
    csv_table.to_csv(csv_path, index=False)

    clients_path = os.path.join(out_dir, "clients.xlsx")
    with pd.ExcelWriter(clients_path, engine="openpyxl", datetime_format="yyyy-mm-dd") as writer:
        client_table.to_excel(writer, sheet_name="Clients", index=False)
        managers.to_excel(writer, sheet_name="Account Managers", index=False)
        _widen(writer.sheets["Clients"], client_table)
        _widen(writer.sheets["Account Managers"], managers)

    invoices_path = os.path.join(out_dir, "invoices.xlsx")
    with pd.ExcelWriter(invoices_path, engine="openpyxl", datetime_format="yyyy-mm-dd") as writer:
        invoices.to_excel(writer, sheet_name="Invoices", index=False, startrow=3)  # header is on row 4
        sheet = writer.sheets["Invoices"]
        sheet["A1"] = "Invoice Register - FY2025 (demo data)"
        sheet["A2"] = "Fictional data for testing. Not real clients."
        _widen(sheet, invoices)

    return [
        ("clients.xlsx", "sheets: Clients, Account Managers", len(client_table)),
        ("engagements.csv", "money and % stored as text", len(csv_table)),
        ("invoices.xlsx", "title rows above the header", len(invoices)),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create fake sample files for Analytics Copilot.")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data"),
                        help="folder to write to (default: sample_data next to this script)")
    parser.add_argument("--clients", type=int, default=40, help="number of clients, 5 to 90 (default 40)")
    parser.add_argument("--seed", type=int, default=7, help="change for different random data (default 7)")
    args = parser.parse_args()

    clients = max(5, min(90, args.clients))
    files = write_files(args.out, clients, args.seed)
    print(f"Created in {args.out}:")
    for name, note, rows in files:
        print(f"  {name:<18} {rows:>5} rows   ({note})")
    print("Upload these files in the app. Client ID links the three files.")


if __name__ == "__main__":
    main()

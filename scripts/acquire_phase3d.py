#!/usr/bin/env python3
"""Phase 3D re-acquisition + expansion (item 13).

Re-acquires the Phase 3C corpora (`.cache/` is snapshot-excluded, so it does not survive
between sessions) and attempts the Phase 3D expansion targets:

    policy wordings  7 -> >= 12
    real bill files 17 -> >= 30
    independent bill families >= 5

Every download is recorded in `.cache/real_data/_acquisition.jsonl` with `source`, `url`,
`sha256`, `bytes` and a status. Nothing is written outside `.cache/real_data/`.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import time

CACHE = pathlib.Path("/home/user/claimcheck/.cache/real_data")
LOG = CACHE / "_acquisition.jsonl"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def record(row: dict) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def get(url: str, dest: pathlib.Path, *, timeout: int = 60, referer: str | None = None,
        insecure: bool = False) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 2000:
        return {"url": url, "path": str(dest), "status": "cached",
                "sha256": sha256(dest), "bytes": dest.stat().st_size}
    cmd = ["curl", "-sSL", "--max-time", str(timeout), "-A", UA]
    if insecure:
        cmd.append("-k")
    if referer:
        cmd += ["-e", referer]
    cmd += ["-o", str(dest), "-w", "%{http_code}", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 20)
        code = (out.stdout or "").strip()
    except subprocess.TimeoutExpired:
        code = "timeout"
    if code != "200" or not dest.exists() or dest.stat().st_size < 2000:
        size = dest.stat().st_size if dest.exists() else 0
        if dest.exists() and dest.stat().st_size < 2000:
            head = dest.read_bytes()[:80]
            dest.unlink(missing_ok=True)
            return {"url": url, "path": str(dest), "status": f"fail:{code}",
                    "bytes": size, "head": head.decode("latin-1", "replace")}
        return {"url": url, "path": str(dest), "status": f"fail:{code}", "bytes": size}
    if dest.suffix.lower() == ".pdf":
        head = dest.read_bytes()[:5]
        if not head.startswith(b"%PDF"):
            dest.unlink(missing_ok=True)
            return {"url": url, "path": str(dest), "status": "fail:not-a-pdf",
                    "head": head.decode("latin-1", "replace")}
    return {"url": url, "path": str(dest), "status": "ok",
            "sha256": sha256(dest), "bytes": dest.stat().st_size}


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hf_file(repo: str, name: str, dest: pathlib.Path) -> dict:
    return get(f"https://huggingface.co/datasets/{repo}/resolve/main/{name}", dest)


# --------------------------------------------------------------------------- bills

BILL_REPO = "sansverse/medical-bill-samples"


def bills() -> list[dict]:
    names = ([f"train_sample_{i}.pdf" for i in list(range(1, 16))]
             + ["train_sample_10_13_merged.pdf", "train_sample_merged.pdf"])
    out = []
    for name in names:
        r = hf_file(BILL_REPO, name, CACHE / "indian_bills" / name)
        r.update(source=BILL_REPO, kind="bill")
        record(r)
        out.append(r)
    return out


def bills_expansion() -> list[dict]:
    """Phase 3D expansion: independent Indian bill families beyond the Phase 3C set."""
    out: list[dict] = []
    extra = {
        # Hugging Face datasets whose file listing is bill-like and Indian.
        "meet9614/medidata": ["README.md"],
    }
    for repo, names in extra.items():
        for name in names:
            dest = CACHE / "indian_bills_expansion" / f"{repo.replace('/', '__')}__{name}"
            r = hf_file(repo, name, dest)
            r.update(source=repo, kind="bill-expansion-probe")
            record(r)
            out.append(r)
    return out


# --------------------------------------------------------------------------- wordings

#: Exact IRDAI Document-Library URLs. The regulator publishes wordings per product as
#: ``/documents/<group>/<folder>/<file>.pdf/<entry-uuid>?version=…&download=true``; the
#: uuid is per-file and is *not* derivable, so every URL here was found by indexed search
#: rather than guessed. A guessed URL returns Liferay's 404 shell — that was Phase 3D's
#: first acquisition failure and is recorded in the report.
IRDAI_WORDINGS: dict[str, str] = {
    # -- Phase 3C wordings, re-acquired ------------------------------------------------
    "uni-complete-healthcare_2023.pdf":
        "https://irdai.gov.in/documents/37343/931203/UNIHLIP23006V032223.pdf/"
        "a78a1bbf-533e-247a-b9a0-136768029158?version=1.0&t=1669354136950&download=true",
    "fgi-health-total_2022.pdf":
        "https://irdai.gov.in/documents/37343/931203/Health+Total.pdf/"
        "1686e404-21d0-da0a-e815-8d1c2e52bc5c?version=1.1&t=1668923517128&download=true",
    "fgi-future-health-suraksha_2021.pdf":
        "https://irdai.gov.in/documents/37343/931203/FGIHLIP21156V022021_2020-2021.pdf/"
        "9d3df075-c8ee-3981-324e-7ad9fa3aec10?version=1.1&t=1668579067754&download=true",
    # -- Phase 3D expansion ------------------------------------------------------------
    "fgi-group-health-sm-mid_2021.pdf":
        "https://irdai.gov.in/documents/37343/931203/FGIHLGP21164V022021_2020-2021.pdf/"
        "849ebe92-3718-e2cd-f707-a1ae59ecdd63?version=1.1&t=1668578856519&download=true",
    "iffco-tokio-group-health_2021.pdf":
        "https://irdai.gov.in/documents/37343/931203/IFFHLGP21327V022021_2020-2021.pdf/"
        "bcc820d1-ffa1-a915-af8c-11aa957b0fb7?version=1.1&t=1668589668244&download=true",
    "liberty-group-health_2021.pdf":
        "https://irdai.gov.in/documents/37343/931203/LIBHLGP21498V022021_2020-2021.pdf/"
        "02c526cb-d2a3-3706-729e-cc645ca06934?version=1.1&t=1668592494622&download=true",
    "united-india-group-health_2021.pdf":
        "https://irdai.gov.in/documents/37343/931203/UIIHLGP21226V022021_2020-2021.pdf/"
        "3761fa7d-ef8c-7d8d-2b7a-2dd44eb41f25?version=1.1&t=1668590224936&download=true",
    "bajaj-allianz-flexi-health-protect-group.pdf":
        "https://irdai.gov.in/documents/37343/931203/Flexi+Health+Protect(Group).pdf/"
        "cc5ba02a-385a-ca3d-a607-61c0a798101e?version=1.1&t=1668753289313&download=true",
    "sbi-superhealth_2023.pdf":
        "https://irdai.gov.in/documents/37343/931203/SBIHLIP23050V012223.pdf/"
        "de34fc22-e5b0-d313-41b1-9bcd4243d600?version=1.0&t=1669351245742&download=true",
    "royal-sundaram-group-arogya-sanjeevani_2021.pdf":
        "https://irdai.gov.in/documents/37343/931203/RSAHLGP21542V012021_2020-2021.pdf/"
        "cf3b91fa-f7ea-563e-6d3c-21ebb034ae1e?version=1.1&t=1668752049109&download=true",
    "care-plus_2022.pdf":
        "https://irdai.gov.in/documents/37343/931203/CHIHLIP22047V012122_HEALTH2082.pdf/"
        "68d457ef-a927-0d1a-df73-4d9f0999822f?version=1.1&t=1668769091888&download=true",
    "care-classic.pdf":
        "https://irdai.gov.in/documents/37343/931203/Care+Classic.pdf/"
        "7be83790-6e88-553b-b72b-a21de4930483?version=1.2&t=1668753400254&download=true",
    "icici-lombard-group-health_2022.pdf":
        "https://irdai.gov.in/documents/37343/931203/ICIHLGP22083V022122_HEALTH2101.pdf/"
        "6b5ac086-3668-d92b-1d2d-0935976be059?version=1.1&t=1668842885975&download=true",
}

#: Phase 3C wordings whose exact entry uuid was NOT re-established. Recorded as attempted
#: with their status rather than quietly dropped: an acquisition that fails is a finding.
IRDAI_WORDINGS_UNAVAILABLE: dict[str, str] = {
    "fgi-group-health_2021.pdf":
        "https://irdai.gov.in/documents/37343/931203/FGIHLGP21165V022021.pdf",
    "care-grameen-care-plus_2022.pdf":
        "https://irdai.gov.in/documents/37343/931203/CHIHMGP22132V012122.pdf",
    "sbi-retail-health_2010.pdf":
        "https://irdai.gov.in/documents/993134/SBIHLIP11002V021011.pdf",
    "bharti-axa-smart-health-essential_2021.pdf":
        "https://irdai.gov.in/documents/993134/BHAHLIP21507V022021.pdf",
}


def wordings() -> list[dict]:
    out = []
    for name, url in IRDAI_WORDINGS.items():
        dest = CACHE / "wordings" / name
        r = get(url, dest, referer="https://irdai.gov.in/")
        r.update(source="irdai.gov.in document library", kind="wording", instrument=name)
        record(r)
        out.append(r)
    for name, url in IRDAI_WORDINGS_UNAVAILABLE.items():
        dest = CACHE / "wordings" / name
        if dest.exists() and dest.stat().st_size > 2000:
            continue                                   # already present from a later route
        r = get(url, dest, referer="https://irdai.gov.in/")
        r.update(source="irdai.gov.in document library (uuid not re-established)",
                 kind="wording", instrument=name, status_detail="entry-uuid-unknown")
        record(r)
        out.append(r)
    return out


# --------------------------------------------------------------------------- regulations

#: Regulation/circular filenames, taken from the mirror's own file listing rather than
#: guessed. Phase 3C established the mirror is a *discovery route only*: provenance for the
#: rule pack is the IRDAI instrument number on the document itself, and the instrument text is
#: identical whichever copy is read. Nothing from the mirror is committed.
IRDAI_REGULATION_FILES: tuple[str, ...] = (
    "Master_Circular_on_Health_Insurance_Business_29052024.pdf",
    "Master_Circular_on_Standardization_of_Health_Insurance_Products.pdf",
    "Master_Circular_on_Standardization_of_Health_Insurance_Products_-_Corrigend.pdf",
    "Guidelines_on_Standardization_of_Exclusions_in_Health_Insurance_Contracts.pdf",
    "Guidelines_on_Standardization_of_General_Terms_and_Clauses_in_Health_Insura.pdf",
    "Master_circular_for_TPAs.pdf",
    "Communication_on_settlement_of_Health_Insurance_Claims.pdf",
    "Communication_on_settlement_of_health_insurance_claims_against_General_Insu.pdf",
    "Delay_in_Claim_Intimation_Documents_Submission.pdf",
    "Consolidated_Guidelines_on_Product_filing_in_Health_Insurance_Business.pdf",
    "The_Insurance_Ombudsman_Rules_2017.pdf",
    "Insurance_Ombudsman_Amendment_Rules_2023.pdf",
    "Annexure_B_-_Data_Template_for_General_and_Health_Insurers.pdf",
    "Additional_Norms_on_portability_under_Health_Insurance_policies.pdf",
    "Communications_on_basic_information_on_health_insurance_policies_to_the_pol.pdf",
    "Master_Circular_on_General_Insurance_Business.pdf",
    "Modified_guidelines_on_Standardization_in_Health_Insurance_Business.pdf",
    "Guidelines_on_Standardization_of_Exclusions_in_Health_Insurance_Contracts1.pdf",
)


def regulations() -> list[dict]:
    out = []
    for name in IRDAI_REGULATION_FILES:
        dest = CACHE / "irdai_corpus" / name
        r = hf_file("Ashishmg10/irdai-corpus", name, dest)
        r.update(source="Ashishmg10/irdai-corpus", kind="regulation",
                 note="mirror; provenance is the instrument number on the document")
        record(r)
        out.append(r)
    return out


# --------------------------------------------------------------------------- misc

MISC = {
    "newindia-claim-form.pdf":
        "https://www.newindia.co.in/uploads/claims/claim_form_health.pdf",
    "iitm-claim-instructions.pdf":
        "https://www.iitm.ac.in/sites/default/files/health_claim_instructions.pdf",
    "iai-health-data-availability.pdf":
        "https://www.actuariesindia.org/sites/default/files/2022-06/"
        "Health_Insurance_India-Data_Availability_and_Applicability.pdf",
    "BaakiBatao-categories.json":
        "https://raw.githubusercontent.com/Chavan-Kartik/BaakiBatao/main/packages/"
        "rulepack/data/v1/categories.json",
    "BaakiBatao-clauses.json":
        "https://raw.githubusercontent.com/Chavan-Kartik/BaakiBatao/main/packages/"
        "rulepack/data/v1/clauses.json",
    "claimback-DATASET.md":
        "https://raw.githubusercontent.com/HitanshGithub/claimback/main/DATASET.md",
}


def misc() -> list[dict]:
    out = []
    for name, url in MISC.items():
        dest = CACHE / "misc" / name
        r = get(url, dest, insecure=("actuariesindia" in url))
        r.update(source=url.split("/")[2], kind="misc")
        record(r)
        out.append(r)
    return out


def main() -> int:
    parts = sys.argv[1:] or ["bills", "wordings", "regulations", "misc"]
    results: dict[str, list[dict]] = {}
    for part in parts:
        fn = {"bills": bills, "wordings": wordings, "regulations": regulations,
              "misc": misc, "bills-expansion": bills_expansion}.get(part)
        if fn is None:
            continue
        results[part] = fn()
        ok = sum(1 for r in results[part] if r["status"] in ("ok", "cached"))
        print(f"[{part}] {ok}/{len(results[part])} ok", flush=True)
        for r in results[part]:
            if r["status"] not in ("ok", "cached"):
                print(f"    FAIL {r['status']:14} {r.get('path','')} {r.get('head','')[:40]}",
                      flush=True)
    print(json.dumps({k: len(v) for k, v in results.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

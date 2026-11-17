"""收集 WingLightGIS 發佈包之第三方授權聲明與套件清單 (tools/collect_licenses.py)。

用法：
    runtime\\python.exe tools\\collect_licenses.py <發佈資料夾路徑>
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 在 conda-forge runtime 中屬於 Copyleft (GPL / LGPL) 或帶 GPL 組態的重要元件與官方原始碼庫
COPYLEFT_UPSTREAM_HINTS: Dict[str, Tuple[str, str]] = {
    "librttopo": ("GPL-2.0-or-later", "https://git.osgeo.org/gitea/rttopo/librttopo | https://github.com/conda-forge/librttopo-feedstock"),
    "lzo": ("GPL-2.0-or-later", "https://www.oberhumer.com/opensource/lzo/ | https://github.com/conda-forge/lzo-feedstock"),
    "libspatialite": ("MPL-1.1 / GPL-2.0-or-later (gpl build)", "https://www.gaia-gis.it/fossil/libspatialite | https://github.com/conda-forge/libspatialite-feedstock"),
    "libarchive": ("BSD-2-Clause / lzo-gpl build", "https://www.libarchive.org/ | https://github.com/conda-forge/libarchive-feedstock"),
    "geos": ("LGPL-2.1-only", "https://libgeos.org/ | https://github.com/conda-forge/geos-feedstock"),
    "libiconv": ("LGPL-2.1-only", "https://www.gnu.org/software/libiconv/ | https://github.com/conda-forge/libiconv-feedstock"),
    "libfreetype": ("GPL-2.0-only OR FTL", "https://freetype.org/ | https://github.com/conda-forge/freetype-feedstock"),
    "libfreetype6": ("GPL-2.0-only OR FTL", "https://freetype.org/ | https://github.com/conda-forge/freetype-feedstock"),
}


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _collect_site_packages_licenses(runtime_dir: Path, out_dir: Path) -> int:
    site_packages = runtime_dir / "Lib" / "site-packages"
    if not site_packages.is_dir():
        return 0
    copied = 0
    for pkg_meta in sorted(site_packages.iterdir()):
        if not pkg_meta.is_dir():
            continue
        if not (pkg_meta.name.endswith(".dist-info") or pkg_meta.name.endswith(".egg-info")):
            continue
        lic_subdir = pkg_meta / "licenses"
        if lic_subdir.is_dir():
            for file in lic_subdir.rglob("*"):
                if file.is_file():
                    rel = file.relative_to(pkg_meta)
                    dest = out_dir / "python_packages" / pkg_meta.name / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, dest)
                    copied += 1
        for file in pkg_meta.iterdir():
            if not file.is_file():
                continue
            upper = file.name.upper()
            if any(token in upper for token in ("LICENSE", "COPYING", "NOTICE")):
                dest = out_dir / "python_packages" / pkg_meta.name / file.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, dest)
                copied += 1
    return copied


def _collect_share_licenses(runtime_dir: Path, out_dir: Path) -> int:
    """保留 Library\\share\\licenses 與 Library\\share\\doc 中可能出現的授權檔（瘦身前可一併保存）。"""
    copied = 0
    for rel_dir in (Path("Library/share/licenses"), Path("Library/share/doc")):
        base = runtime_dir / rel_dir
        if not base.is_dir():
            continue
        for file in base.rglob("*"):
            if not file.is_file():
                continue
            upper = file.name.upper()
            if any(tok in upper for tok in ("LICENSE", "COPYING", "NOTICE", "COPYRIGHT", "README")):
                rel = file.relative_to(base)
                dest = out_dir / "conda_share_licenses" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, dest)
                copied += 1
    return copied


def _read_conda_packages(runtime_dir: Path) -> List[Dict[str, str]]:
    meta_dir = runtime_dir / "conda-meta"
    packages: List[Dict[str, str]] = []
    if not meta_dir.is_dir():
        return packages
    for json_path in sorted(meta_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        packages.append(
            {
                "name": str(data.get("name", "")),
                "version": str(data.get("version", "")),
                "build": str(data.get("build", "")),
                "license": str(data.get("license", "") or "(未標示)"),
                "url": str(data.get("url", "")),
            }
        )
    packages.sort(key=lambda item: item["name"].lower())
    return packages


def collect_all_licenses(release_dir: Path, runtime_dir: Path) -> None:
    release_dir = Path(release_dir).resolve()
    runtime_dir = Path(runtime_dir).resolve()
    lic_dir = release_dir / "第三方授權"
    lic_dir.mkdir(parents=True, exist_ok=True)

    # 1. WingLightGIS 本體 MIT 授權
    _copy_if_exists(PROJECT_ROOT / "LICENSE", release_dir / "LICENSE.txt")
    _copy_if_exists(PROJECT_ROOT / "LICENSE", lic_dir / "WingLightGIS_MIT_LICENSE.txt")

    # 2. 版本清單與 Python 授權
    _copy_if_exists(PROJECT_ROOT / "RUNTIME_VERSIONS.txt", lic_dir / "RUNTIME_VERSIONS.txt")
    _copy_if_exists(runtime_dir / "LICENSE_PYTHON.txt", lic_dir / "PYTHON_PSF_LICENSE.txt")

    # 3. Python 套件授權檔與 share 授權檔
    py_count = _collect_site_packages_licenses(runtime_dir, lic_dir)
    share_count = _collect_share_licenses(runtime_dir, lic_dir)

    # 4. 完整 conda-meta 套件授權清冊
    conda_pkgs = _read_conda_packages(runtime_dir)
    table_lines = [
        "WingLightGIS 獨立執行環境 (./runtime) Conda 套件授權清冊",
        "=" * 78,
        f"共計 {len(conda_pkgs)} 個套件；來源頻道主要為 conda-forge。",
        "",
        f"{'套件名稱':<26} {'版本':<14} {'建置組態':<20} {'授權條款'}",
        "-" * 78,
    ]
    for pkg in conda_pkgs:
        table_lines.append(f"{pkg['name']:<26} {pkg['version']:<14} {pkg['build']:<20} {pkg['license']}")
    table_lines.append("")
    table_lines.append("套件下載與原始碼建置來源 (URL / Conda-Forge Feedstock):")
    table_lines.append("-" * 78)
    for pkg in conda_pkgs:
        feedstock = f"https://github.com/conda-forge/{pkg['name']}-feedstock"
        table_lines.append(f"{pkg['name']} {pkg['version']}: {pkg['url'] or '(local)'} | {feedstock}")

    (lic_dir / "conda套件授權清單.txt").write_text("\n".join(table_lines) + "\n", encoding="utf-8")

    # 5. Copyleft (GPL / LGPL) 與原始碼取得總說明
    pkg_by_name = {pkg["name"]: pkg for pkg in conda_pkgs}
    readme_lines = [
        "WingLightGIS 第三方開放原始碼授權與原始碼取得說明",
        "=" * 72,
        "",
        "一、WingLightGIS 主程式授權",
        "  - main.py、core\\、ui\\、launcher\\ 與本專案原始碼採用 MIT License。",
        "  - 完整條款見本發佈包根目錄之 LICENSE.txt。",
        "",
        "二、獨立沙盒 ./runtime 內的第三方元件",
        "  - ./runtime 由 conda-forge 官方預先編譯之公開二進位套件建置而成，包含 Python、GDAL、PROJ、",
        "    WhiteboxTools、CustomTkinter、TkinterDnD2、Folium、NumPy、Pillow、OpenPyXL 等。",
        "  - 其中部分相依原生物件屬於 GPL / LGPL 等 Copyleft 授權（含以 GPL 組態建置之相依元件）：",
        "",
    ]
    for name, (lic_text, url_hint) in COPYLEFT_UPSTREAM_HINTS.items():
        found = pkg_by_name.get(name)
        ver = f" v{found['version']} ({found['build']})" if found else ""
        readme_lines.append(f"    * {name}{ver}")
        readme_lines.append(f"      授權條款: {lic_text}")
        readme_lines.append(f"      原始碼與建置腳本取得: {url_hint}")
        readme_lines.append("")

    readme_lines.extend(
        [
            "三、其他套件與 Python 套件授權檔位置",
            f"  - Python 套件隨附授權文字檔已收錄於：第三方授權\\python_packages\\（共 {py_count} 份檔案）",
            f"  - Library\\share 隨附授權檔收錄於：第三方授權\\conda_share_licenses\\（共 {share_count} 份檔案）",
            "  - 所有 conda 套件精確版本與 conda-forge feedstock / 下載網址：第三方授權\\conda套件授權清單.txt",
            "  - 建置規範版本清單：第三方授權\\RUNTIME_VERSIONS.txt",
            "  - 所有 conda-forge 套件的對應原始碼壓縮檔與建置 recipe 皆可由上述 conda-forge GitHub Feedstock",
            "    與 https://conda-forge.org/ 免費公開取得。",
        ]
    )
    (lic_dir / "README_第三方授權與原始碼取得說明.txt").write_text("\n".join(readme_lines) + "\n", encoding="utf-8")
    print(f"[collect_licenses] 已寫入授權文件至 {lic_dir}（conda 套件 {len(conda_pkgs)} 個，Python 授權檔 {py_count} 份）")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "dist" / "WingLightGIS_綠色免安裝版"
    runtime = target / "runtime"
    if not runtime.is_dir():
        runtime = PROJECT_ROOT / "runtime"
    collect_all_licenses(target, runtime)


if __name__ == "__main__":
    main()

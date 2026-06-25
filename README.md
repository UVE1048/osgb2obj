# OSGB → H8-OBJ

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)

**OSGB oblique photography tiles → H3 Level 8 merged OBJ converter.**

Converts ContextCapture / RealityCapture / 重建大师 OSGB tile sets into OBJ format merged by **H3 Level 8 grid cells**, with coordinate transformation, texture extraction, auto-computed normals, and spatial index metadata.

---

## 🇨🇳 中文说明

### 功能

- ✅ **OSGB 解析** — 使用 `osgconv` 提取几何（顶点、UV、三角面）
- ✅ **纹理提取** — 从 OSGB 二进制直接搜索 JPEG 标记 (`FF D8 FF`) 提取贴图
- ✅ **坐标转换** — 上海地方坐标系 → EPSG:3857（float64 双精度，避免 0.5m 精度损失）
- ✅ **H3 Level 8 网格合并** — 同一网格内模型合并，网格质心作为局部坐标偏移原点
- ✅ **法线计算** — 自动计算 smooth vertex normals（osgconv 不输出法线）
- ✅ **多材质支持** — 每瓦片独立 `usemtl`，各自引用纹理
- ✅ **空间索引** — 每个网格输出 `metadata.xml`（含 SRSOrigin / H3Cell / WGS84Center）
- ✅ **进度条 + 续跑** — 实时显示进度/ETA，支持 `--resume` 跳过已转换瓦片

### 管线流程

```
OSGB 原始坐标 (X_local, Y_local, Z)
  ↓ + SRSOrigin
上海投影坐标 (北京54/高斯-克吕格, 中央经线 121.467°)
  ↓ pyproj (float64)
WGS84 经纬度
  ↓ pyproj
EPSG:3857 (Web墨卡托)
  ↓ - H8网格质心
OBJ 局部坐标
```

### 安装

```bash
sudo apt install -y openscenegraph unrar
pip3 install numpy pyproj h3 Pillow

git clone https://github.com/UVE1048/osgb2obj.git
cd osgb2obj
```

### 使用

```bash
# 全量处理
python3 osgb2h8obj.py --workers 4

# 测试模式（先跑20个瓦片验证）
python3 osgb2h8obj.py --test 20

# 续跑模式（跳过 osgconv，只做合并）
python3 osgb2h8obj.py --resume
```

### 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--input / -i` | 输入 OSGB 目录 | `Data/` |
| `--output / -o` | 输出目录 | `output_h8obj/` |
| `--work-dir / -w` | 临时工作目录 | `work_temp/` |
| `--h3-level` | H3 网格级别 | `8` |
| `--workers` | 并行线程数 | `4` |
| `--test N` | 只处理前 N 个瓦片 | 全量 |
| `--resume` | 从 work_temp 续跑 | — |

---

## 🇬🇧 English

### Features

- **OSGB parsing** via `osgconv` — geometry (vertices, UVs, faces)
- **Texture extraction** — search JPEG markers (`FF D8 FF`) directly from OSGB binary
- **Coordinate transformation** — Shanghai local → EPSG:3857 (float64, sub-mm precision)
- **H3 Level 8 grid merging** — merge tiles in the same cell, centroid-relative local coordinates
- **Smooth normals** — auto-computed (osgconv omits normals in OBJ output)
- **Multi-material** — each tile gets its own `usemtl` with texture reference
- **Spatial index** — `metadata.xml` per cell (SRSOrigin / H3Cell / WGS84Center)
- **Progress bar + resume** — real-time progress/ETA, `--resume` skips completed tiles

### Pipeline

```
OSGB raw coords (X_local, Y_local, Z)
  ↓ + SRSOrigin
Shanghai projected (Beijing 1954 / Gauss-Kruger, central meridian 121.467°)
  ↓ pyproj (float64)
WGS84 lat/lng
  ↓ pyproj
EPSG:3857 (Web Mercator)
  ↓ - H8 cell centroid
OBJ local coords
```

### Installation

```bash
sudo apt install -y openscenegraph unrar
pip3 install numpy pyproj h3 Pillow

git clone https://github.com/UVE1048/osgb2obj.git
cd osgb2obj
```

### Usage

```bash
# Full processing
python3 osgb2h8obj.py --workers 4

# Test mode (first N tiles)
python3 osgb2h8obj.py --test 20

# Resume mode (skip osgconv, redo merging)
python3 osgb2h8obj.py --resume
```

### Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--input / -i` | OSGB data directory | `Data/` |
| `--output / -o` | Output directory | `output_h8obj/` |
| `--work-dir / -w` | Working temp directory | `work_temp/` |
| `--h3-level` | H3 grid level | `8` |
| `--workers` | Parallel workers | `4` |
| `--test N` | Process first N tiles only | all |
| `--resume` | Resume from work_temp | — |

---

## Output Structure

```
output_h8obj/
├── 88309b9XXXXXXXX/               # H3 Level 8 cell
│   ├── model.obj                  # Merged mesh (float64, centroid-offset)
│   ├── model.mtl                  # Per-tile materials
│   ├── metadata.xml               # Spatial index
│   └── textures/
│       ├── tile_tex.jpg
│       └── ...
└── summary.json                   # Processing summary
```

### metadata.xml Format

```xml
<?xml version="1.0" encoding="utf-8"?>
<ModelMetadata version="1">
    <SRS>EPSG:3857</SRS>
    <SRSOrigin>13488547.9281,3619802.6757,21.4908</SRSOrigin>
    <H3Cell>88309b99e5fffff</H3Cell>
    <WGS84Center>121.16968764,30.90025928</WGS84Center>
    <TileCount>5</TileCount>
    <Texture><ColorSource>Visible</ColorSource></Texture>
</ModelMetadata>
```

> OBJ coordinates = actual EPSG:3857 coordinates − SRSOrigin (relative to cell centroid)

---

## Performance Reference

| Metric | Value |
|--------|-------|
| Input tiles | 14,507 L22 tiles |
| Total time | ~270s (4 workers) |
| Per tile avg | ~0.036s |
| Output H3 Level 8 cells | 20 |
| Largest cell | 2,434 tiles / 4.8M verts / 6.5M faces |
| Largest cell merge | ~40s |
| Total output | ~15GB |

---

## Common Pitfalls

| Problem | Solution |
|---------|----------|
| `osgconv` omits normals | Script auto-computes smooth vertex normals |
| Texture name conflicts | Each tile uses isolated work dir; output always overwrites |
| Large cell merge timeout | Vectorized normals (`np.add.at`); use `--resume` for 2-phase run |
| Coordinate precision loss | `float64` throughout; OBJ writes `{:.15f}` |
| Source CRS not Shanghai | Modify `SRC_CRS` in script (e.g. `pyproj.CRS.from_epsg(32650)`) |

---

## License

MIT License — see [LICENSE](LICENSE).

## Author

**UV E1048** ([@UVE1048](https://github.com/UVE1048))

## Hermes Agent Skill

This project ships as a [Hermes Agent](https://hermes-agent.nousresearch.com) skill.  
If you use Hermes Agent, load it with `skill_view(name='osgb-to-obj-h8')`.

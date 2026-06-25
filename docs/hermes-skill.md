---
name: osgb-to-obj-h8
description: OSGB倾斜摄影瓦片 → H3 Level 8网格合并OBJ 转换管线（坐标转换、纹理提取、法线计算、metadata.xml生成）
tags: [osgb, obj, h3, oblique-photography, gis, 3d]
related_skills: [3d-tile-conversion]
author: UVE1048
repo: https://github.com/UVE1048/osgb2obj
license: MIT
---

# OSGB → H8-Merged OBJ 转换管线

将 ContextCapture / 重建大师 等倾斜摄影建模软件产出的 **OSGB 瓦片**（L22 叶子节点）转换为按 **H3 Level 8 网格** 合并的 **OBJ 格式**，保留贴图纹理、自动计算法线、生成空间索引 metadata.xml。

**输出可直接用于**：Cesium / Three.js / Blender / Unity / Unreal Engine 等三维引擎。

---

## 安装

### 系统依赖

```bash
sudo apt install -y openscenegraph unrar
```

> `osgconv`（来自 OpenSceneGraph）用于解析 OSGB 二进制为 OBJ。  
> `unrar` 用于解压 .rar 格式的原始数据包。

### Python 依赖

```bash
pip3 install numpy pyproj h3 Pillow
```

| 包 | 用途 |
|-----|------|
| `numpy` | 矢量化的顶点/法线计算 |
| `pyproj` | 坐标转换（上海地方↔WGS84↔EPSG:3857） |
| `h3` | H3 网格编码（Level 8 索引与分桶） |
| `Pillow` | 纹理文件验证 |

### 获取脚本

```bash
git clone https://github.com/UVE1048/osgb2obj.git
cd osgb2obj
```

---

## 快速开始

```bash
# 全量处理（建议 4 线程）
python3 osgb2h8obj.py --workers 4

# 测试模式（只处理前 N 个瓦片，验证管线）
python3 osgb2h8obj.py --test 20

# 从 work_temp 续跑（跳过 osgconv，仅重做合并）
python3 osgb2h8obj.py --resume

# 指定输入输出目录
python3 osgb2h8obj.py \
  --input /path/to/Data \
  --output /path/to/output \
  --work-dir /tmp/work_temp
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--input` / `-i` | OSGB 数据目录（含 `Tile_+XXX_+YYY/` 子目录） | `Data/` |
| `--output` / `-o` | 输出目录 | `output_h8obj/` |
| `--work-dir` / `-w` | 中间文件临时目录 | `work_temp/` |
| `--h3-level` | H3 网格级别 | `8` |
| `--workers` | 并行线程数 | `4` |
| `--test N` | 仅处理前 N 个瓦片（调试用） | 全量 |
| `--resume` | 从 work_temp 续跑（跳过 osgconv） | — |

---

## 管线流程

### 1️⃣ 坐标转换链

```
OSGB 原始坐标 (X_local, Y_local, Z)
  ↓ + SRSOrigin (从 metadata.xml读取)
上海投影坐标 (北京54/高斯-克吕格, 中央经线 121.467°)
  ↓ pyproj.Transformer (always_xy=True, float64)
WGS84 经纬度
  ↓ pyproj.Transformer
EPSG:3857 (Web墨卡托, float64 避免 0.5m 精度损失)
  ↓ - 网格质心偏移
OBJ 局部坐标 ← 适用于渲染引擎
```

**精度保障**：全程 `np.float64`，OBJ 输出 `{:.15f}`，在 ~13,500,000 的 3857 坐标量级保持亚毫米精度。

### 2️⃣ 纹理提取

OSGB 二进制文件内嵌 JPEG 纹理。直接从二进制流搜索 JPEG 标记提取：

```python
SOF_MARKER = b'\xff\xd8\xff'  # JPEG Start Of Image
EOI_MARKER = b'\xff\xd9'      # End Of Image
```

- 每文件**仅提取第一张 JPEG**（主纹理）
- 自动根据 OSGB 头部的文件名信息命名（fallback: `{tile_name}_texture.jpg`）
- 覆盖写入，不留旧缓存

### 3️⃣ 法线计算

`osgconv` 的 OBJ writer **默认不输出法线**（面格式 `f v/vt/`），脚本自动计算 **smooth vertex normals**：

```python
# 完全向量化
face_normals = np.cross(v1 - v0, v2 - v0)        # 面法线
np.add.at(vertex_normals, face_idx, weighted)      # scatter-add 累加
vertex_normals /= np.linalg.norm(vertex_normals)   # 归一化
```

- 面法线按三角形面积加权累加到顶点
- 最大网格（6.5M 三角面）约 **40s** 完成

### 4️⃣ H3 Level 8 网格合并

- 每瓦片的几何质心 → WGS84 → `h3.latlng_to_cell(lat, lon, 8)`
- 同一网格的瓦片合并为一个 OBJ
- **每瓦片独立材质**（`usemtl mat_N`），各自引用自己的纹理
- 网格质心作为局部偏移原点写入 metadata.xml 的 `SRSOrigin`

---

## 输入输出结构

### 输入目录

```
Data/
├── Tile_+XXX_+YYY/
│   ├── Tile_+XXX_+YYY.osgb           # 根节点
│   ├── Tile_+XXX_+YYY_L14_0.osgb     # LOD 层级
│   ├── ...
│   └── Tile_+XXX_+YYY_L22_*.osgb     # 叶子节点（处理目标）
└── ...
```

脚本自动匹配 `*_L22_*.osgb` 模式，仅处理最高 LOD 的叶子节点。

### 输出目录

```
output_h8obj/
├── 88309b9XXXXXXXXX/             # H3 Level 8 网格目录
│   ├── model.obj                 # 合并 OBJ (float64, 局部偏移坐标)
│   ├── model.mtl                 # 材质文件（每瓦片独立材质）
│   ├── metadata.xml              # 空间索引
│   └── textures/
│       ├── tile_1_tex.jpg
│       └── ...
├── summary.json                  # 处理汇总（含每个网格的统计）
└── ...
```

### metadata.xml 格式

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

> OBJ 中的坐标 = 实际 EPSG:3857 坐标 − SRSOrigin（即相对于网格质心的局部坐标）

---

## 坐标系适配

如果数据**不是**上海地方坐标系（北京54/高斯-克吕格），修改脚本中的 `SRC_CRS` 定义：

```python
# 示例：WGS84 / UTM zone 50N
SRC_CRS = pyproj.CRS.from_epsg(32650)

# 示例：自定义投影
SRC_CRS = pyproj.CRS.from_proj4(
    "+proj=tmerc +lat_0=0 +lon_0=114 +k=1 +x_0=500000 +y_0=0 +ellps=WGS84 +units=m"
)
```

---

## 性能参考

| 指标 | 数据 |
|------|------|
| 输入瓦片数 | 14,507 个 L22 瓦片 |
| 总处理时间 | ~270s（4 线程并行） |
| 单瓦片平均耗时 | ~0.036s |
| 输出 H3 Level 8 网格数 | 20 个 |
| 最大网格 | 2,434 瓦片 / 4.8M 顶点 / 6.5M 三角面 |
| 最大网格合并耗时 | ~40s |
| 总输出体积 | ~15GB / 22GB（含 work_temp） |

---

## 常见坑点

### 🚩 osgconv 不输出法线
**现象**：OBJ 面格式为 `f v/vt/`，缺少 `vn`。  
**解决**：`compute_smooth_normals()` 自动计算补充。

### 🚩 osgconv DAE 写入失败但无报错
**现象**：提示 "Data written" 但文件不存在。  
**解决**：放弃 DAE 方案，直接从 OSGB 二进制提取 JPEG 纹理（`FF D8 FF` 搜索）。

### 🚩 纹理文件名冲突
**现象**：不同瓦片同名纹理导致 `xxx_1.jpg`、`xxx_2.jpg` 污染。  
**解决**：每个瓦片使用独立 work 目录 + 输出时始终覆盖（fresh extract）。

### 🚩 大网格合并超时
**现象**：2400+ 瓦片的合并超过 600s 终端超时。  
**解决**：
1. 向量化法线计算（`np.add.at` 替代 Python 循环）
2. 分两阶段：先 `--workers 4` 转 tiles，再用 `--resume` 专跑合并
3. 合并时检查 `model.obj` 已存在则跳过

### 🚩 坐标精度丢失
**现象**：3857 坐标 ~13,500,000 量级，float32 精度约 0.5m。  
**解决**：全程 `float64` + OBJ 输出 `{:.15f}`。

---

## 验证方式

```bash
# 检查法线数量
grep -c "^vn " output_h8obj/*/model.obj

# 检查纹理引用
grep "map_Kd" output_h8obj/*/model.mtl

# 反向验证坐标
python3 -c "
import pyproj
lon, lat = pyproj.Transformer.from_crs(3857, 4326, always_xy=True).transform(
    13488547.92, 3619802.67)
print(f'位置: {lon:.6f}°E, {lat:.6f}°N')
"

# 验证纹理可读
python3 -c "
from PIL import Image
import glob
for f in glob.glob('output_h8obj/*/textures/*.jpg'):
    img = Image.open(f)
    img.verify()
    print(f'✅ {f} ({img.size})')
"
```

## GitHub 仓库

完整代码：**[github.com/UVE1048/osgb2obj](https://github.com/UVE1048/osgb2obj)**

包含文件：
- `osgb2h8obj.py` — 主转换脚本（891 行，MIT License）
- `README.md` — 使用文档（中英文双语）
- `LICENSE` — MIT 协议

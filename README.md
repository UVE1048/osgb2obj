# OSGB → H8-OBJ 倾斜摄影模型转换工具

将倾斜摄影 OSGB 瓦片数据转换为按 **H3 Level 8 网格** 合并的 OBJ 格式，支持坐标转换、纹理提取、法线计算。

## 功能

- ✅ **OSGB 解析** — 使用 `osgconv` 提取几何信息（顶点、UV、面）
- ✅ **纹理提取** — 从 OSGB 二进制直接搜索 JPEG 标记提取贴图（无需额外工具）
- ✅ **坐标转换** — 上海地方坐标系 → WGS84 → EPSG:3857（float64 精度）
- ✅ **H3 网格合并** — Level 8 网格分组合并，同网格内模型用同一坐标偏移
- ✅ **法线计算** — 自动计算 smooth vertex normals（osgconv 不输出法线）
- ✅ **多材质支持** — 每瓦片独立材质（usemtl），各自引用纹理
- ✅ **空间索引** — `metadata.xml` 含 SRSOrigin / H3Cell / WGS84Center
- ✅ **进度条** — 实时显示转换进度和预估剩余时间

## 环境要求

```bash
# 系统
sudo apt install openscenegraph unrar

# Python
pip3 install numpy pyproj h3 Pillow
```

## 快速开始

```bash
python3 osgb2h8obj.py --workers 4
```

### 命令参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--input` | 输入 OSGB 数据目录 | `input_data/Data` |
| `--output` | 输出目录 | `output_h8obj` |
| `--work-dir` | 临时工作目录 | `work_temp` |
| `--h3-level` | H3 网格级别 | `8` |
| `--workers` | 并行线程数 | `4` |
| `--test N` | 只处理前 N 个瓦片（测试用） | 全量 |
| `--resume` | 从 work_temp 续跑（跳过 osgconv） | — |

## 输入输出结构

### 输入
```
Data/
├── Tile_+XXX_+YYY/
│   ├── Tile_+XXX_+YYY.osgb            # 根节点
│   ├── Tile_+XXX_+YYY_L14_0.osgb      # LOD层级
│   ├── ...
│   └── Tile_+XXX_+YYY_L22_*.osgb      # 叶子节点（处理目标）
└── ...
```

### 输出
```
output_h8obj/
├── 88309b9XXXXXXXXX/               # H3 Level 8 网格
│   ├── model.obj                   # 合并模型（float64，局部偏移坐标）
│   ├── model.mtl                   # 材质文件（每瓦片独立材质）
│   ├── metadata.xml                # 空间索引
│   └── textures/
│       ├── tile_1_tex.jpg
│       └── ...
├── summary.json                    # 处理汇总
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

OBJ 中的坐标 = 实际 EPSG:3857 坐标 − SRSOrigin

## 坐标系处理

```
上海地方坐标（北京54/高斯-克吕格，中央经线121.467°）
  → 加 SRSOrigin（metadata.xml读取）
  → pyproj 转 WGS84
  → EPSG:3857（Web墨卡托，float64避免0.5m精度损失）
  → H3质心偏移 → OBJ局部坐标
```

如需适配其他坐标系，修改脚本中的 `SRC_CRS` 定义。

## 性能参考

| 规模 | 耗时 |
|------|------|
| 14,507 个 L22 瓦片 | ~270s (4线程) |
| 单个瓦片平均 | ~0.036s |
| 最大网格合并 (2,434 tiles, 4.4M verts) | ~30s |
| 总输出 | 15GB / 20 H3网格 |

## 脚本位置

`./osgb2obj/osgb2h8obj.py`

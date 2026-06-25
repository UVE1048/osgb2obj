#!/usr/bin/env python3
"""
OSGB to OBJ Converter with H3 Level 8 Grid Merge
=================================================
Converts OSGB oblique photography tiles to OBJ format:
  - Extracts geometry (v, vt, vn, f) via osgconv
  - Extracts embedded JPEG textures from OSGB binary
  - Transforms coordinates: Shanghai local → EPSG:3857 (float64 precision)
  - Groups by H3 Level 8 grid cell
  - Merges all tiles in each cell
  - Applies centroid offset for local OBJ coordinates
  - Generates metadata.xml per cell

Usage:
  python3 osgb2h8obj.py [--input INPUT_DIR] [--output OUTPUT_DIR]
                        [--h3-level 8] [--workers 4] [--test N]
"""

import os, sys, subprocess, json, struct, re, shutil, glob
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
import pyproj
import h3
from collections import defaultdict
import argparse
import time
import xml.etree.ElementTree as ET
from xml.dom import minidom


# ============================================================
# PROGRESS BAR
# ============================================================

class Progress:
    """Simple progress bar with ETA, no external dependencies."""
    def __init__(self, total, prefix='', width=40):
        self.total = total
        self.prefix = prefix
        self.width = width
        self.start_time = time.time()
        self.n = 0
        self._last_print = 0
    
    def update(self, n=1):
        self.n += n
        now = time.time()
        if now - self._last_print < 0.2 and self.n < self.total:
            return
        self._last_print = now
        self._display()
    
    def _display(self):
        elapsed = time.time() - self.start_time
        pct = self.n / self.total if self.total > 0 else 0
        filled = int(self.width * pct)
        bar = '█' * filled + '░' * (self.width - filled)
        
        if self.n > 0:
            rate = self.n / elapsed if elapsed > 0 else 0
            eta = (self.total - self.n) / rate if rate > 0 else 0
            eta_str = f"ETA {eta:.0f}s" if eta < 3600 else f"ETA {eta/60:.1f}m"
        else:
            eta_str = ""
        
        print(f"\r{self.prefix} |{bar}| {self.n}/{self.total} ({pct*100:.0f}%) {eta_str}    ", end='', flush=True)
        if self.n >= self.total:
            print()
    
    def close(self):
        self.n = self.total
        self._display()


# ============================================================
# CONFIGURATION
# ============================================================

# Source CRS definition (from metadata.xml)
# Shanghai local Gauss-Kruger, Beijing 1954 datum
SRC_CRS = pyproj.CRS.from_proj4(
    "+proj=tmerc +lat_0=0 +lon_0=121.4671519444444 +k=1.0 "
    "+x_0=8 +y_0=-3457143.04 +ellps=krass +units=m +no_defs"
)
TGT_CRS = pyproj.CRS.from_epsg(3857)
COORD_TRANSFORMER = pyproj.Transformer.from_crs(SRC_CRS, TGT_CRS, always_xy=True)

# SRSOrigin from metadata.xml
SRS_ORIGIN_X = -29674.93821
SRS_ORIGIN_Y = -40464.15255
SRS_ORIGIN_Z = 0.0

H3_LEVEL = 8

# JPEG markers
SOF_MARKER = b'\xff\xd8\xff'
EOI_MARKER = b'\xff\xd9'


# ============================================================
# HELPER: Extract embedded JPEGs from OSGB binary
# ============================================================

def extract_textures_from_osgb(osgb_path, output_dir):
    """
    Extract embedded JPEG textures from an OSGB binary file.
    Only extracts the FIRST JPEG per file (the primary texture).
    Returns dict {texture_filename: absolute_path} or empty dict.
    """
    os.makedirs(output_dir, exist_ok=True)
    textures = {}
    
    try:
        with open(osgb_path, 'rb') as f:
            data = f.read()
    except Exception as e:
        print(f"  [WARN] Cannot read {osgb_path}: {e}")
        return textures
    
    # Find the FIRST JPEG only (primary texture)
    start = data.find(SOF_MARKER)
    if start < 0:
        return textures
    end = data.find(EOI_MARKER, start)
    if end < 0:
        return textures
    
    jpeg_data = data[start:end + 2]
    if len(jpeg_data) < 100:
        return textures
    
    # Look for filename before the JPEG (in the OSGB header)
    filename = None
    search_start = max(0, start - 200)
    name_area = data[search_start:start]
    
    name_match = re.search(rb'([A-Za-z0-9_+.-]+\.(?:jpg|jpeg|png|dds|bmp))', name_area)
    if name_match:
        raw_name = name_match.group(1).decode('ascii', errors='replace')
        filename = raw_name
    else:
        base = os.path.splitext(os.path.basename(osgb_path))[0]
        filename = f"{base}_texture.jpg"
    
    out_path = os.path.join(output_dir, filename)
    # Overwrite existing file (fresh extraction)
    with open(out_path, 'wb') as f:
        f.write(jpeg_data)
    
    textures[filename] = out_path
    return textures


# ============================================================
# HELPER: Parse OBJ file
# ============================================================

def parse_obj(obj_path):
    """
    Parse a Wavefront OBJ file.
    Returns dict with:
      - vertices: np.array(N, 3) float64
      - texcoords: np.array(M, 2) float64 or None
      - normals: np.array(K, 3) float64 or None
      - faces: list of [v1,v2,v3] (0-indexed)
      - face_tex: list of [t1,t2,t3] or None
      - face_norm: list of [n1,n2,n3] or None
      - mtllib: str or None
      - usemtl: str or None
    """
    verts = []
    tcs = []
    norms = []
    faces = []
    face_tc = []
    face_n = []
    mtllib = None
    usemtl = None
    
    with open(obj_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            cmd = parts[0]
            
            if cmd == 'v' and len(parts) >= 4:
                verts.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif cmd == 'vt' and len(parts) >= 3:
                tcs.append([float(parts[1]), float(parts[2])])
            elif cmd == 'vn' and len(parts) >= 4:
                norms.append([float(parts[1]), float(parts[2]), float(parts[3])])
            elif cmd == 'f':
                face_verts = []
                face_tcs = []
                face_ns = []
                for p in parts[1:]:
                    v_idx = None
                    t_idx = None
                    n_idx = None
                    sub = p.split('/')
                    v_idx = int(sub[0]) - 1  # 1-indexed to 0-indexed
                    if len(sub) >= 2 and sub[1]:
                        t_idx = int(sub[1]) - 1
                    if len(sub) >= 3 and sub[2]:
                        n_idx = int(sub[2]) - 1
                    face_verts.append(v_idx)
                    face_tcs.append(t_idx)
                    face_ns.append(n_idx)
                
                # Triangulate: split N-gon into triangles (fan triangulation)
                for i in range(1, len(face_verts) - 1):
                    faces.append([face_verts[0], face_verts[i], face_verts[i+1]])
                    if face_tcs[0] is not None:
                        face_tc.append([face_tcs[0], face_tcs[i], face_tcs[i+1]])
                    if face_ns[0] is not None:
                        face_n.append([face_ns[0], face_ns[i], face_ns[i+1]])
            elif cmd == 'mtllib':
                mtllib = parts[1]
            elif cmd == 'usemtl':
                usemtl = ' '.join(parts[1:])
    
    result = {
        'vertices': np.array(verts, dtype=np.float64) if verts else np.zeros((0,3), dtype=np.float64),
        'texcoords': np.array(tcs, dtype=np.float64) if tcs else None,
        'normals': np.array(norms, dtype=np.float64) if norms else None,
        'faces': faces,
        'face_tex': face_tc if face_tc else None,
        'face_norm': face_n if face_n else None,
        'mtllib': mtllib,
        'usemtl': usemtl,
    }
    return result


# ============================================================
# STEP 1: Convert coordinates
# ============================================================

def transform_vertices(vertices):
    """
    Transform vertices from Shanghai local → EPSG:3857.
    Input: np.array(N, 3) in local coordinates (X_local, Y_local, Z)
    Output: np.array(N, 3) in EPSG:3857
    
    Formula:
      proj_x = local_x + SRS_ORIGIN_X
      proj_y = local_y + SRS_ORIGIN_Y
      (proj_x, proj_y) → (x3857, y3857) via pyproj
    """
    n = vertices.shape[0]
    result = np.zeros_like(vertices)
    
    # Step 1: Add SRSOrigin to get Shanghai projected coordinates
    proj_x = vertices[:, 0] + SRS_ORIGIN_X
    proj_y = vertices[:, 1] + SRS_ORIGIN_Y
    z = vertices[:, 2] + SRS_ORIGIN_Z
    
    # Step 2: Transform projected → EPSG:3857
    x3857_list, y3857_list = COORD_TRANSFORMER.transform(proj_x.tolist(), proj_y.tolist())
    
    result[:, 0] = np.array(x3857_list, dtype=np.float64)
    result[:, 1] = np.array(y3857_list, dtype=np.float64)
    result[:, 2] = z  # Z stays as height (no CRS transformation for Z in 2D transform)
    
    return result


# ============================================================
# STEP 2: Process single OSGB file
# ============================================================

def process_single_tile(osgb_path, work_dir, tile_index, total_tiles):
    """
    Process one OSGB leaf tile.
    Returns dict with:
    """
    tile_name = os.path.basename(osgb_path)
    
    base = os.path.splitext(tile_name)[0]
    tile_work = os.path.join(work_dir, base)
    os.makedirs(tile_work, exist_ok=True)
    
    # --- Extract textures from OSGB binary ---
    textures = extract_textures_from_osgb(osgb_path, tile_work)
    
    # --- Convert to OBJ using osgconv ---
    obj_path = os.path.join(tile_work, f"{base}.obj")
    
    # Run osgconv
    result = subprocess.run(
        ['osgconv', osgb_path, obj_path],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        print(f"  [WARN] osgconv failed for {tile_name}: {result.stderr.strip()}")
        return None
    if not os.path.exists(obj_path):
        print(f"  [WARN] OBJ not created for {tile_name}")
        return None
    
    # --- Parse OBJ ---
    obj_data = parse_obj(obj_path)
    if len(obj_data['faces']) == 0:
        print(f"  [WARN] No faces in {tile_name}, skipping")
        return None
    
    verts_local = obj_data['vertices']
    if verts_local.shape[0] == 0:
        print(f"  [WARN] No vertices in {tile_name}, skipping")
        return None
    
    # --- Transform coordinates ---
    verts_3857 = transform_vertices(verts_local)
    
    # --- Compute centroid for H3 indexing ---
    centroid_3857 = verts_3857.mean(axis=0)
    cx, cy, cz = centroid_3857[0], centroid_3857[1], centroid_3857[2]
    
    # --- Convert centroid to lat/lng for H3 ---
    # EPSG:3857 → WGS84
    wgs84 = pyproj.CRS.from_epsg(4326)
    trans = pyproj.Transformer.from_crs(TGT_CRS, wgs84, always_xy=True)
    lon, lat = trans.transform(cx, cy)
    
    # --- Compute H3 cell ---
    try:
        h3_cell = h3.latlng_to_cell(lat, lon, H3_LEVEL)
    except Exception as e:
        print(f"  [WARN] H3 failed for {tile_name}: {e}")
        return None
    
    # Read the mtl/lib for texture filename
    usemtl = obj_data['usemtl']
    # Also check for the original mtl to get texture filename
    mtl_texture_filename = None
    mtl_path = os.path.join(os.path.dirname(obj_path), f"{base}.mtl")
    if os.path.exists(mtl_path):
        with open(mtl_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('map_Kd') or line.startswith('map_Ka'):
                    parts = line.split()
                    if len(parts) >= 2:
                        mtl_texture_filename = parts[-1].strip()
    
    return {
        'h3_cell': h3_cell,
        'vertices_3857': verts_3857,
        'texcoords': obj_data['texcoords'],
        'normals': obj_data['normals'],
        'faces': obj_data['faces'],
        'face_tex': obj_data['face_tex'],
        'face_norm': obj_data['face_norm'],
        'usemtl': usemtl or obj_data['mtllib'],
        'textures': textures,
        'mtl_texture_filename': mtl_texture_filename,
        'tile_name': tile_name,
        'centroid_3857': (cx, cy, cz),
    }


# ============================================================
# STEP 2.5: Compute normals from geometry
# ============================================================

def compute_smooth_normals(vertices, faces):
    """
    Compute smooth vertex normals from triangle geometry (fully vectorized).
    
    Args:
        vertices: np.array(N, 3) vertex positions
        faces: list of [v1, v2, v3] (0-indexed triangle vertex indices)
    
    Returns:
        normals: np.array(N, 3) smoothed vertex normals
    """
    n_verts = vertices.shape[0]
    n_faces = len(faces)
    
    if n_faces == 0:
        return np.zeros((n_verts, 3), dtype=np.float64)
    
    # Build face index array
    face_idx = np.array(faces, dtype=np.int32)  # (N, 3)
    
    # Compute face normals via cross product (vectorized)
    v0 = vertices[face_idx[:, 0]]
    v1 = vertices[face_idx[:, 1]]
    v2 = vertices[face_idx[:, 2]]
    
    face_normals = np.cross(v1 - v0, v2 - v0)  # (N, 3)
    face_areas = np.linalg.norm(face_normals, axis=1)
    
    # Normalize face normals (avoid divide by zero)
    face_normals_unit = np.zeros_like(face_normals)
    valid = face_areas > 1e-10
    if np.any(valid):
        face_normals_unit[valid] = face_normals[valid] / face_areas[valid, np.newaxis]
    
    # Scatter-add: accumulate weighted face normals to vertex normals
    weights = np.where(valid, face_areas, 0.0)[:, np.newaxis]  # (N, 1)
    weighted_normals = face_normals_unit * weights  # (N, 3)
    
    vertex_normals = np.zeros((n_verts, 3), dtype=np.float64)
    for k in range(3):
        np.add.at(vertex_normals, face_idx[:, k], weighted_normals)
    
    # Normalize
    lengths = np.linalg.norm(vertex_normals, axis=1)
    valid_verts = lengths > 1e-10
    vertex_normals[valid_verts] /= lengths[valid_verts, np.newaxis]
    vertex_normals[~valid_verts] = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    
    return vertex_normals


# ============================================================
# STEP 3: Merge tiles within same H3 cell
# ============================================================

def merge_tiles_in_cell(h3_cell, tile_results, output_dir):
    """
    Merge all tile results in one H3 cell into a single OBJ.
    Supports multiple textures/materials: each tile's faces are grouped by its own texture.
    """
    cell_dir = os.path.join(output_dir, h3_cell)
    textures_dir = os.path.join(cell_dir, 'textures')
    
    # Clean any existing output first (fresh start)
    if os.path.exists(cell_dir):
        shutil.rmtree(cell_dir)
    os.makedirs(cell_dir, exist_ok=True)
    os.makedirs(textures_dir, exist_ok=True)
    
    # Deduplicate texture files by filename (tiles have unique names already)
    used_texture_filenames = set()
    
    merged_verts = []
    merged_tcs = []
    merged_norms = []
    
    # Track face groups: (material_name, faces, face_tex, face_norm, tex_keys)
    face_groups = []
    total_verts = 0
    total_tcs = 0
    total_norms = 0
    
    for r_idx, r in enumerate(tile_results):
        if r is None:
            continue
        
        nv = r['vertices_3857'].shape[0]
        merged_verts.append(r['vertices_3857'])
        
        if r['texcoords'] is not None:
            merged_tcs.append(r['texcoords'])
        if r['normals'] is not None:
            merged_norms.append(r['normals'])
        
        # Copy texture files (unique per tile)
        for tex_name, tex_path in r['textures'].items():
            dest = os.path.join(textures_dir, tex_name)
            if not os.path.exists(dest):
                shutil.copy2(tex_path, dest)
            used_texture_filenames.add(tex_name)
        
        # Material name and texture references
        tex_keys = list(r['textures'].keys())
        if tex_keys:
            mat_name = f"mat_{r_idx}"
        else:
            mat_name = f"mat_{r_idx}_notexture"
        
        # Adjust face indices and group by material
        group_faces = []
        group_face_tc = []
        group_face_n = []
        for i, face in enumerate(r['faces']):
            adj_face = [v + total_verts for v in face]
            group_faces.append(adj_face)
            if r['face_tex'] is not None and i < len(r['face_tex']):
                group_face_tc.append([t + total_tcs for t in r['face_tex'][i]])
            if r['face_norm'] is not None and i < len(r['face_norm']):
                group_face_n.append([n + total_norms for n in r['face_norm'][i]])
        
        face_groups.append((
            mat_name,
            group_faces,
            group_face_tc if group_face_tc else None,
            group_face_n if group_face_n else None,
            tex_keys,
        ))
        
        total_verts += nv
        total_tcs += r['texcoords'].shape[0] if r['texcoords'] is not None else 0
        total_norms += r['normals'].shape[0] if r['normals'] is not None else 0
    
    if len(merged_verts) == 0:
        return None
    
    # Concatenate arrays
    all_verts = np.vstack(merged_verts) if len(merged_verts) > 1 else merged_verts[0]
    all_tcs = np.vstack(merged_tcs) if merged_tcs and len(merged_tcs) > 1 else (merged_tcs[0] if merged_tcs else None)
    all_norms = np.vstack(merged_norms) if merged_norms and len(merged_norms) > 1 else (merged_norms[0] if merged_norms else None)
    
    # Compute cell centroid offset
    cell_centroid = all_verts.mean(axis=0)
    offset_x, offset_y, offset_z = cell_centroid[0], cell_centroid[1], cell_centroid[2]
    
    # Apply offset: vertices become local to cell centroid
    all_verts_local = all_verts - cell_centroid[np.newaxis, :]
    
    # --- Compute smooth normals for merged mesh ---
    # osgconv's OBJ writer doesn't output normals, so we compute them
    all_faces_flat = []
    for _, group_faces, _, _, _ in face_groups:
        all_faces_flat.extend(group_faces)
    
    all_norms = compute_smooth_normals(all_verts_local, all_faces_flat)
    
    # Wrap face_groups with normal info: each vertex has one smooth normal,
    # so normal index = vertex index
    face_groups_with_normals = []
    for mat_name, group_faces, group_face_tc, _, tex_keys in face_groups:
        group_face_n = [face[:] for face in group_faces]  # copy, use vertex indices
        face_groups_with_normals.append((
            mat_name, group_faces, group_face_tc, group_face_n, tex_keys
        ))
    
    # --- Write OBJ with usemtl groups ---
    obj_path = os.path.join(cell_dir, 'model.obj')
    with open(obj_path, 'w') as f:
        f.write(f"# Merged OBJ for H3 cell {h3_cell}\n")
        f.write(f"# Original tiles: {len(tile_results)}\n")
        f.write(f"# Vertices: {all_verts.shape[0]}, Faces: {sum(len(g[1]) for g in face_groups_with_normals)}\n")
        f.write(f"# Normals: auto-computed smooth normals\n")
        f.write(f"# EPSG:3857 centroid offset: {offset_x:.10f}, {offset_y:.10f}, {offset_z:.10f}\n")
        f.write("mtllib model.mtl\n\n")
        
        # Write vertices (local, with offset)
        for row in all_verts_local:
            f.write(f"v {row[0]:.15f} {row[1]:.15f} {row[2]:.15f}\n")
        f.write("\n")
        
        # Write texcoords
        if all_tcs is not None:
            for row in all_tcs:
                f.write(f"vt {row[0]:.15f} {row[1]:.15f}\n")
            f.write("\n")
        
        # Write normals
        for row in all_norms:
            f.write(f"vn {row[0]:.10f} {row[1]:.10f} {row[2]:.10f}\n")
        f.write("\n")
        
        # Write faces grouped by material
        for mat_name, group_faces, group_face_tc, group_face_n, tex_keys in face_groups_with_normals:
            f.write(f"usemtl {mat_name}\n")
            
            if group_face_tc and group_face_n:
                for i, face in enumerate(group_faces):
                    tc = group_face_tc[i]
                    # Normal index = vertex index (smooth normals indexed by vertex)
                    fn = face  # reuse vertex indices for normal
                    f.write(f"f {face[0]+1}/{tc[0]+1}/{fn[0]+1} {face[1]+1}/{tc[1]+1}/{fn[1]+1} {face[2]+1}/{tc[2]+1}/{fn[2]+1}\n")
            elif group_face_tc:
                for i, face in enumerate(group_faces):
                    tc = group_face_tc[i]
                    f.write(f"f {face[0]+1}/{tc[0]+1} {face[1]+1}/{tc[1]+1} {face[2]+1}/{tc[2]+1}\n")
            else:
                for face in group_faces:
                    f.write(f"f {face[0]+1} {face[1]+1} {face[2]+1}\n")
    
    # --- Write MTL with per-tile materials ---
    mtl_path = os.path.join(cell_dir, 'model.mtl')
    with open(mtl_path, 'w') as f:
        for mat_name, _, _, _, tex_keys in face_groups:
            f.write(f"newmtl {mat_name}\n")
            f.write("  Ka 1.0 1.0 1.0\n")
            f.write("  Kd 1.0 1.0 1.0\n")
            f.write("  Ks 0.0 0.0 0.0\n")
            f.write("  Ns 0.0\n")
            f.write("  illum 1\n")
            if tex_keys:
                tex_rel = f"textures/{tex_keys[0]}"
                f.write(f"  map_Kd {tex_rel}\n")
            f.write("\n")
    
    # --- Write metadata.xml ---
    metadata_path = os.path.join(cell_dir, 'metadata.xml')
    # Convert cell centroid (3857) to lat/lng for reference
    wgs84 = pyproj.CRS.from_epsg(4326)
    trans = pyproj.Transformer.from_crs(TGT_CRS, wgs84, always_xy=True)
    lon, lat = trans.transform(offset_x, offset_y)
    
    xml_str = f'''<?xml version="1.0" encoding="utf-8"?>
<ModelMetadata version="1">
    <!--Spatial Reference System-->
    <SRS>EPSG:3857</SRS>
    <!--Origin in Spatial Reference System-->
    <SRSOrigin>{offset_x:.15f},{offset_y:.15f},{offset_z:.15f}</SRSOrigin>
    <!--H3 Level 8 Cell-->
    <H3Cell>{h3_cell}</H3Cell>
    <!--Cell center (WGS84) for reference-->
    <WGS84Center>{lon:.8f},{lat:.8f}</WGS84Center>
    <!--Number of merged tiles-->
    <TileCount>{len(tile_results)}</TileCount>
    <Texture>
        <ColorSource>Visible</ColorSource>
    </Texture>
</ModelMetadata>'''
    
    with open(metadata_path, 'w') as f:
        f.write(xml_str)
    
    return {
        'h3_cell': h3_cell,
        'vertex_count': all_verts.shape[0],
        'face_count': sum(len(g[1]) for g in face_groups),
        'tile_count': len(tile_results),
        'centroid_3857': (offset_x, offset_y, offset_z),
        'output_dir': cell_dir,
    }


# ============================================================
# MAIN
# ============================================================

def find_leaf_tiles(input_dir):
    """Find all L22 leaf OSGB files."""
    pattern = os.path.join(input_dir, '**', '*_L22_*.osgb')
    files = sorted(glob.glob(pattern, recursive=True))
    return files


def main():
    parser = argparse.ArgumentParser(description='OSGB to H8-merged OBJ converter')
    parser.add_argument('--input', '-i', 
                       default='Data',
                       help='Input OSGB data directory (with Tile_+XXX_+YYY/ subdirs)')
    parser.add_argument('--output', '-o',
                       default='output_h8obj',
                       help='Output directory')
    parser.add_argument('--work-dir', '-w',
                       default='work_temp',
                       help='Working directory for intermediate files')
    parser.add_argument('--h3-level', type=int, default=8,
                       help='H3 grid level (default: 8)')
    parser.add_argument('--workers', type=int, default=4,
                       help='Number of parallel workers (default: 4)')
    parser.add_argument('--test', type=int, default=None,
                       help='Only process N tiles for testing')
    parser.add_argument('--resume', action='store_true',
                       help='Skip already-processed tiles')
    args = parser.parse_args()
    
    input_dir = args.input
    output_dir = args.output
    work_dir = args.work_dir
    global H3_LEVEL
    H3_LEVEL = args.h3_level
    
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(work_dir, exist_ok=True)
    
    # Clean work_dir subdirs from previous runs (each tile gets fresh work dir)
    for item in os.listdir(work_dir):
        item_path = os.path.join(work_dir, item)
        if os.path.isdir(item_path):
            try:
                shutil.rmtree(item_path)
            except:
                pass
    
    # Find leaf tiles
    print("=== Finding leaf (L22) OSGB tiles ===")
    leaf_tiles = find_leaf_tiles(input_dir)
    print(f"Found {len(leaf_tiles)} L22 tiles")
    
    if args.test:
        leaf_tiles = leaf_tiles[:args.test]
        print(f"TEST MODE: processing first {len(leaf_tiles)} tiles")
    
    if len(leaf_tiles) == 0:
        print("No leaf tiles found!")
        sys.exit(1)
    
    # Process each tile
    print(f"\n=== Converting {len(leaf_tiles)} tiles ({args.workers} workers) ===")
    start_time = time.time()
    
    tile_results = []
    
    # In resume mode, re-load from work_temp instead of converting again
    if args.resume:
        print("Resume mode: scanning existing work_temp for tile data...")
        # We need to re-process OBJ files from work_temp
        # This is much faster than osgconv
        def reload_from_work(tile_path, work_dir, idx, total):
            tile_name = os.path.basename(tile_path)
            tile_work = os.path.join(work_dir, tile_name)
            obj_path = os.path.join(tile_work, f"{tile_name}.obj")
            if not os.path.exists(obj_path):
                return process_single_tile(tile_path, work_dir, idx, total)
            
            # Parse existing OBJ
            base = os.path.splitext(tile_name)[0]
            obj_data = parse_obj(obj_path)
            if len(obj_data['faces']) == 0 or obj_data['vertices'].shape[0] == 0:
                return None
            
            verts_local = obj_data['vertices']
            verts_3857 = transform_vertices(verts_local)
            
            if verts_3857.shape[0] == 0:
                return None
            
            centroid_3857 = verts_3857.mean(axis=0)
            cx, cy, cz = centroid_3857[0], centroid_3857[1], centroid_3857[2]
            
            wgs84 = pyproj.CRS.from_epsg(4326)
            trans = pyproj.Transformer.from_crs(TGT_CRS, wgs84, always_xy=True)
            lon, lat = trans.transform(cx, cy)
            
            try:
                h3_cell = h3.latlng_to_cell(lat, lon, H3_LEVEL)
            except:
                return None
            
            # Re-extract textures
            textures = extract_textures_from_osgb(tile_path, tile_work)
            
            # Read mtl for texture filename
            mtl_path = os.path.join(tile_work, f"{base}.mtl")
            mtl_tex = None
            if os.path.exists(mtl_path):
                with open(mtl_path) as f:
                    for line in f:
                        if line.startswith('map_Kd'):
                            parts = line.split()
                            if len(parts) >= 2:
                                mtl_tex = parts[-1].strip()
            
            return {
                'h3_cell': h3_cell,
                'vertices_3857': verts_3857,
                'texcoords': obj_data['texcoords'],
                'normals': obj_data['normals'],
                'faces': obj_data['faces'],
                'face_tex': obj_data['face_tex'],
                'face_norm': obj_data['face_norm'],
                'usemtl': obj_data['usemtl'] or obj_data['mtllib'],
                'textures': textures,
                'mtl_texture_filename': mtl_tex,
                'tile_name': tile_name,
                'centroid_3857': (cx, cy, cz),
            }
        
        # Sequential reload (fast - just OBJ parsing)
        reload_prog = Progress(len(leaf_tiles), prefix='  Reloading', width=40)
        for i, tile_path in enumerate(leaf_tiles):
            result = reload_from_work(tile_path, work_dir, i + 1, len(leaf_tiles))
            if result:
                tile_results.append(result)
            reload_prog.update()
        reload_prog.close()
        
        print(f"  Reloaded {len(tile_results)} tiles from work_temp")
    elif args.workers > 1:
        # Parallel processing with progress
        prog = Progress(len(leaf_tiles), prefix='  Converting', width=40)
        total_submitted = 0
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {}
            for i, tile_path in enumerate(leaf_tiles):
                # Check if already processed (resume mode)
                tile_name = os.path.basename(tile_path)
                tile_work = os.path.join(work_dir, tile_name)
                obj_in_work = os.path.join(tile_work, f"{tile_name}.obj")
                if args.resume and os.path.exists(obj_in_work):
                    continue  # Skip already-processed tiles
                
                future = executor.submit(
                    process_single_tile, 
                    tile_path, work_dir, prog.n + 1, len(leaf_tiles)
                )
                futures[future] = tile_path
                total_submitted += 1
            
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=300)
                    if result:
                        tile_results.append(result)
                except Exception as e:
                    tile_path = futures[future]
                    pass
                prog.update()
        prog.close()
        print(f"  Processed {len(tile_results)} tiles ({total_submitted} submitted)")
    else:
        # Sequential processing with progress bar
        prog = Progress(len(leaf_tiles), prefix='  Converting')
        for i, tile_path in enumerate(leaf_tiles):
            result = process_single_tile(tile_path, work_dir, i + 1, len(leaf_tiles))
            if result:
                tile_results.append(result)
            prog.update()
        prog.close()
    
    elapsed = time.time() - start_time
    print(f"\n=== Conversion complete: {len(tile_results)}/{len(leaf_tiles)} tiles processed in {elapsed:.1f}s ===")
    
    # Group by H3 cell
    cell_groups = defaultdict(list)
    for r in tile_results:
        cell_groups[r['h3_cell']].append(r)
    
    print(f"\n  → {len(cell_groups)} H3 cells found")
    total_tiles_in_cells = sum(len(v) for v in cell_groups.values())
    min_t, max_t = min(len(v) for v in cell_groups.values()), max(len(v) for v in cell_groups.values())
    print(f"  → Tiles per cell: {min_t} ~ {max_t} (avg {total_tiles_in_cells//max(1,len(cell_groups))})")
    
    # Merge tiles in each cell
    print(f"\n=== Merging cells ===\n")
    merge_results = []
    merged_cells = sorted(cell_groups.items())
    merge_prog = Progress(len(merged_cells), prefix='  Merging', width=30)
    for cell, tiles in merged_cells:
        cell_dir = os.path.join(output_dir, cell)
        cell_obj = os.path.join(cell_dir, 'model.obj')
        
        # Skip cells that already have a valid model.obj (resume mode)
        if os.path.exists(cell_obj) and os.path.getsize(cell_obj) > 1000:
            merge_prog.update()
            continue
        
        result = merge_tiles_in_cell(cell, tiles, output_dir)
        if result:
            merge_results.append(result)
        merge_prog.update()
    merge_prog.close()
    
    # Final summary
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Input tiles:     {len(leaf_tiles)}")
    print(f"Processed:       {len(tile_results)}")
    print(f"H3 Level 8 cells: {len(cell_groups)}")
    print(f"Merged output:   {len(merge_results)} dirs in {output_dir}")
    print(f"Total time:      {elapsed:.1f}s")
    print(f"{'='*60}")
    
    # Write summary JSON
    summary_path = os.path.join(output_dir, 'summary.json')
    summary = {
        'input_tiles': len(leaf_tiles),
        'processed_tiles': len(tile_results),
        'h3_cells': len(cell_groups),
        'merge_results': merge_results,
        'total_time_s': elapsed,
        'h3_level': H3_LEVEL,
        'crs': 'EPSG:3857',
    }
    # Convert numpy types for JSON
    class NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            return super().default(obj)
    
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2, cls=NumpyEncoder)
    
    print(f"\nSummary written to {summary_path}")
    print("Done!")


if __name__ == '__main__':
    main()

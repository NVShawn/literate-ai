"""Illustrative 3D scenes for the film, rendered by Blender; never screen captures.

blender -b --factory-startup -P blender_scenes.py -- SCENE OUTPUT_DIR [FRAMES]

Writes OUTPUT_DIR/0001.png ... at 1280x720, 24 fps. Generic shapes only: no logos,
trademarks, product likenesses or real people. Labels name roles, not brands.
"""

import math
import random
import sys

import bpy

TEAL, AMBER, RED, NAVY, STEEL = (
    (0.216, 0.847, 0.718),
    (1.0, 0.737, 0.42),
    (0.95, 0.33, 0.30),
    (0.063, 0.098, 0.176),
    (0.55, 0.6, 0.68),
)


def reset(frames):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = 1280, 720
    scene.render.fps = 24
    scene.frame_start, scene.frame_end = 1, frames
    scene.view_settings.view_transform = "Standard"
    scene.eevee.taa_render_samples = 32
    world = bpy.data.worlds.new("night")
    scene.world = world
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    background.inputs[0].default_value = (*NAVY, 1)
    background.inputs[1].default_value = 0.25
    return scene


def material(name, color, emission=0.0, rough=0.45, metal=0.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    bsdf.inputs["Emission Color"].default_value = (*color, 1)
    bsdf.inputs["Emission Strength"].default_value = emission
    return mat


def glow_keys(mat, keys):
    """Animate emission color/strength: keys are (frame, color, strength)."""
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    for frame, color, strength in keys:
        bsdf.inputs["Emission Color"].default_value = (*color, 1)
        bsdf.inputs["Base Color"].default_value = (*color, 1)
        bsdf.inputs["Emission Strength"].default_value = strength
        for socket in ("Emission Color", "Base Color", "Emission Strength"):
            bsdf.inputs[socket].keyframe_insert("default_value", frame=frame)


def box(name, size, location, mat, bevel=0.03):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.scale = [s / 2 for s in size]
    bpy.ops.object.transform_apply(scale=True)
    if bevel:
        mod = obj.modifiers.new("bevel", "BEVEL")
        mod.width, mod.segments = bevel, 4
    obj.data.materials.append(mat)
    return obj


def label(value, location, size, mat, tilt=80):
    bpy.ops.object.text_add(location=location, rotation=(math.radians(tilt), 0, 0))
    obj = bpy.context.object
    obj.data.body = value
    obj.data.size = size
    obj.data.align_x = "CENTER"
    obj.data.extrude = 0.01
    obj.data.materials.append(mat)
    return obj


def visible(obj, keys):
    """Show/hide an object at frames: keys are (frame, shown)."""
    for frame, shown in keys:
        obj.hide_render = not shown
        obj.keyframe_insert("hide_render", frame=frame)


def floor_and_lights(scene, rim=TEAL):
    box("floor", (90, 90, 0.2), (0, 0, -0.1), material("floor", NAVY, rough=0.35), 0)
    bpy.ops.object.light_add(type="AREA", location=(0, -6, 8))
    key = bpy.context.object
    key.data.energy, key.data.size = 900, 10
    key.rotation_euler = (math.radians(40), 0, 0)
    bpy.ops.object.light_add(type="AREA", location=(-7, 4, 5))
    back = bpy.context.object
    back.data.energy, back.data.color = 220, rim
    back.rotation_euler = (math.radians(-60), math.radians(-30), 0)


def camera(scene, start, end, rotation, lens=34):
    bpy.ops.object.camera_add(location=start, rotation=rotation)
    cam = bpy.context.object
    cam.data.lens = lens
    cam.keyframe_insert("location", frame=scene.frame_start)
    cam.location = end
    cam.keyframe_insert("location", frame=scene.frame_end)
    scene.camera = cam


def machines(frames):
    """One gate on three generic machines; each ring lights as its run passes."""
    scene = reset(frames)
    body = material("body", STEEL, rough=0.3, metal=0.8)
    dark = material("screen", (0.02, 0.03, 0.06), rough=0.2)
    floor_and_lights(scene)
    hosts = [("this host", -3.9, "laptop"), ("Linux worker", 0.0, "tower")]
    hosts.append(("Windows worker", 3.9, "desktop"))
    teal = material("teal", TEAL, emission=1.3)
    amber = material("amber", AMBER, emission=0.8)
    for index, (name, x, kind) in enumerate(hosts):
        if kind == "laptop":
            box(name + " base", (2.6, 1.8, 0.12), (x, 0, 0.06), body)
            lid = box(name + " lid", (2.6, 0.1, 1.7), (x, 0.95, 0.88), body)
            lid.rotation_euler[0] = math.radians(-12)
            screen = box(name + " screen", (2.35, 0.02, 1.45), (x, 0.88, 0.9), dark)
            screen.rotation_euler[0] = math.radians(-12)
        elif kind == "tower":
            box(name + " case", (1.3, 2.0, 2.6), (x, 0.3, 1.3), body)
            for slot in range(4):
                box(
                    f"{name} bay {slot}",
                    (1.0, 0.02, 0.12),
                    (x, -0.71, 2.2 - slot * 0.3),
                    dark,
                    0.01,
                )
        else:
            box(name + " stand", (0.3, 0.3, 0.9), (x, 0.6, 0.45), body)
            box(name + " monitor", (2.8, 0.12, 1.7), (x, 0.55, 1.75), body)
            box(name + " screen", (2.6, 0.02, 1.5), (x, 0.48, 1.75), dark)
        done = int(frames * (0.25 + 0.22 * index))
        ring_mat = material(f"ring {index}", AMBER, emission=0.8)
        glow_keys(
            ring_mat,
            [
                (1, AMBER, 0.8),
                (done - 1, AMBER, 0.8),
                (done + 6, TEAL, 1.6),
                (frames, TEAL, 1.3),
            ],
        )
        bpy.ops.mesh.primitive_torus_add(
            location=(x, -1.6, 0.05), major_radius=1.25, minor_radius=0.045
        )
        bpy.context.object.data.materials.append(ring_mat)
        label(name, (x, -3.35, 0.02), 0.36, ring_mat, tilt=55)
        running = label("RUNNING", (x, 0.2, 2.75), 0.36, amber)
        passed = label("PASS", (x, 0.2, 2.75), 0.36, teal)
        visible(running, [(1, True), (done, False)])
        visible(passed, [(1, False), (done, True)])
    camera(scene, (0, -13.5, 5.4), (0, -12.2, 5.0), (math.radians(72), 0, 0))
    return scene


def overnight(frames):
    """Candidates travel overnight through promise, proof and receipt."""
    scene = reset(frames)
    floor_and_lights(scene)
    rng = random.Random(42)
    star = material("star", (0.85, 0.9, 1.0), emission=3)
    for _ in range(140):
        bpy.ops.mesh.primitive_ico_sphere_add(
            radius=rng.uniform(0.02, 0.05),
            subdivisions=1,
            location=(rng.uniform(-30, 30), rng.uniform(25, 40), rng.uniform(4, 22)),
        )
        bpy.context.object.data.materials.append(star)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=2.2, location=(14, 38, 16))
    bpy.context.object.data.materials.append(
        material("moon", (0.95, 0.93, 0.85), emission=1.4)
    )
    track = material("track", (0.13, 0.19, 0.29), rough=0.3, metal=0.4)
    box("track", (16, 0.9, 0.08), (0, 0, 0.04), track, 0.02)
    stations = [("PROMISE", -4.5), ("PROOF", 0.0), ("RECEIPT", 4.5)]
    for index, (name, x) in enumerate(stations):
        lit = int(frames * (0.3 + 0.2 * index))
        mat = material(f"station {index}", AMBER, emission=0.4)
        glow_keys(
            mat,
            [
                (1, AMBER, 0.4),
                (lit - 1, AMBER, 0.4),
                (lit + 6, TEAL, 1.8),
                (frames, TEAL, 1.4),
            ],
        )
        box(
            name, (1.8, 1.8, 1.2), (x, 1.7, 0.6), material("plinth", STEEL, 0, 0.3, 0.7)
        )
        box(name + " lamp", (1.6, 0.06, 0.08), (x, 0.78, 1.1), mat, 0.01)
        label(name, (x, 1.7, 1.6), 0.42, mat)
    candidate = material("candidate", TEAL, emission=2.0)
    for index in range(4):
        bpy.ops.mesh.primitive_cube_add(size=0.45, location=(-8.5, 0, 0.33))
        cube = bpy.context.object
        cube.data.materials.append(candidate)
        start = 1 + index * frames // 5
        cube.keyframe_insert("location", frame=start)
        cube.location.x = 8.5
        cube.keyframe_insert("location", frame=start + int(frames * 0.75))
    camera(scene, (0, -12.0, 4.6), (0, -10.6, 4.2), (math.radians(76), 0, 0), 32)
    return scene


def queue(frames):
    """Push and pray: changes wait in one line; the verdict arrives last."""
    scene = reset(frames)
    floor_and_lights(scene, rim=AMBER)
    track = material("track", (0.13, 0.19, 0.29), rough=0.3, metal=0.4)
    box("track", (22, 0.9, 0.08), (0, 0, 0.04), track, 0.02)
    arch = material("arch", STEEL, rough=0.3, metal=0.7)
    box("left post", (0.3, 0.3, 3.0), (4.0, -0.8, 1.5), arch)
    box("right post", (0.3, 0.3, 3.0), (4.0, 0.8, 1.5), arch)
    box("lintel", (0.4, 2.0, 0.4), (4.0, 0, 3.0), arch)
    lamp = material("lamp", AMBER, emission=1.0)
    keys = []
    for step in range(0, frames + 1, 24):
        keys += [(max(1, step), AMBER, 1.6), (max(1, step + 12), AMBER, 0.3)]
    glow_keys(lamp, keys + [(frames - 20, AMBER, 0.3), (frames - 14, RED, 1.1)])
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.25, location=(4.0, 0, 3.45))
    bpy.context.object.data.materials.append(lamp)
    label("CI", (4.0, -0.25, 3.7), 0.5, material("ci", (0.9, 0.93, 0.97), 0.6))
    waiting = material("waiting", (0.45, 0.52, 0.62), emission=0.15)
    for index in range(10):
        bpy.ops.mesh.primitive_cube_add(size=0.6, location=(2.8 - index * 1.1, 0, 0.38))
        block = bpy.context.object
        block.data.materials.append(waiting)
        block.keyframe_insert("location", frame=1)
        block.location.x += 1.1
        block.keyframe_insert("location", frame=frames)
    verdict = material("verdict", AMBER, emission=0.6)
    glow_keys(
        verdict,
        [
            (1, AMBER, 0.6),
            (frames - 16, AMBER, 0.6),
            (frames - 10, RED, 1.1),
            (frames, RED, 1.1),
        ],
    )
    bpy.ops.mesh.primitive_cube_add(size=0.6, location=(3.9, 0, 0.38))
    head = bpy.context.object
    head.data.materials.append(verdict)
    head.keyframe_insert("location", frame=1)
    head.location.x = 5.6
    head.keyframe_insert("location", frame=frames - 18)
    camera(
        scene,
        (-3.0, -11.5, 4.4),
        (-1.6, -10.4, 4.1),
        (math.radians(76), 0, math.radians(-8)),
        32,
    )
    return scene


SCENES = {"machines": machines, "overnight": overnight, "queue": queue}

if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1 :]
    name, output = argv[0], argv[1]
    frames = int(argv[2]) if len(argv) > 2 else 192
    scene = SCENES[name](frames)
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = output.rstrip("/") + "/"
    bpy.ops.render.render(animation=True)

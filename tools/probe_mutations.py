"""Etapa 0 (complemento) - ¿Dónde quedan los originales de fotos editadas? SOLO LECTURA.
Solo cuenta entradas por extensión bajo /PhotoData/Mutations y /DCIM (archivos sin extensión).
No imprime nombres de archivo."""
import asyncio, json, posixpath, sys
from collections import Counter

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from probe_device import ReadOnlyAfc  # noqa: E402


async def walk(ro, root, limit=20000):
    exts, dirs, sizes, n = Counter(), 0, 0, 0
    stack = [root]
    depth_ex = []
    while stack and n < limit:
        d = stack.pop()
        try:
            names = await ro.listdir(d)
        except Exception:
            continue
        for nm in names:
            p = posixpath.join(d, nm)
            try:
                st = await ro.stat(p)
            except Exception:
                continue
            n += 1
            if st.get("st_ifmt") == "S_IFDIR":
                dirs += 1
                stack.append(p)
            else:
                exts[posixpath.splitext(nm)[1].lower()] += 1
                sizes += int(st.get("st_size", 0))
                if len(depth_ex) < 3 and nm.lower().endswith((".heic", ".jpg", ".jpeg", ".mov")):
                    depth_ex.append(p.count("/"))
    return {"dirs": dirs, "files_by_ext": dict(exts.most_common()), "bytes": sizes,
            "visited": n, "truncated": bool(stack), "sample_media_depths": depth_ex}


async def main():
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService
    out = {}
    lockdown = await create_using_usbmux(autopair=False)
    async with lockdown:
        async with AfcService(lockdown) as afc:
            ro = ReadOnlyAfc(afc)
            for root in ("/PhotoData/Mutations", "/PhotoData/Videos"):
                try:
                    out[root] = await walk(ro, root)
                except Exception as e:  # noqa: BLE001
                    out[root] = {"error": f"{type(e).__name__}: {e}"}
            noext = []
            out["dcim_top_level_files"] = []
            for f in await ro.listdir("/DCIM"):
                fst = await ro.stat(posixpath.join("/DCIM", f))
                if fst.get("st_ifmt") != "S_IFDIR":
                    out["dcim_top_level_files"].append(
                        {"ext": posixpath.splitext(f)[1].lower(), "size": fst.get("st_size")})
                    continue
                for nm in await ro.listdir(posixpath.join("/DCIM", f)):
                    if not posixpath.splitext(nm)[1]:
                        st = await ro.stat(posixpath.join("/DCIM", f, nm))
                        noext.append({"is_dir": st.get("st_ifmt") == "S_IFDIR", "size": st.get("st_size")})
            out["dcim_entries_without_extension"] = noext
    print(json.dumps(out, indent=2))


asyncio.run(main())

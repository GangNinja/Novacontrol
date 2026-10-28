import asyncio, json
from novacontrol.desktop.controller import LocalDesktopRunner
from novacontrol.novaforge.launcher import GameCatalog

async def main():
    runner = LocalDesktopRunner()
    catalog = GameCatalog(
        steam_games=getattr(runner, "installed_steam_games", None),
        shortcuts=getattr(runner, "installed_app_targets", None),
        processes=getattr(runner, "game_processes", None),
        processes_under=getattr(runner, "game_processes_under", None),
        configured_dirs=(),
    )
    rows = await catalog.installed()
    for row in rows:
        art = (row.get("artifact") or {})
        print(json.dumps({"title": row.get("title"), "status": row.get("status"),
                          "source": art.get("source"), "kind": art.get("launch_kind"),
                          "target": art.get("launch_target"), "exe_exists": art.get("executable_exists"),
                          "launchable": art.get("launchable")}))
    await runner.aclose()

asyncio.run(main())

"""清理腾讯系后台进程的小工具（双击 清理后台.bat 运行）。"""

from __future__ import annotations

import subprocess
import time


def snapshot() -> list[tuple[str, int, float]]:
    """返回 [(进程名, 数量, 占用MB)]。"""
    names = ["QQ", "QQKit", "Weixin", "WeChatAppEx", "WeChat"]
    result = []
    for name in names:
        output = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        rows = [line for line in output.splitlines() if name.lower() in line.lower()]
        total = 0.0
        for row in rows:
            parts = [p.strip('"') for p in row.split('","')]
            if len(parts) >= 5:
                mem = parts[4].replace(" K", "").replace(",", "").strip()
                try:
                    total += int(mem) / 1024
                except ValueError:
                    pass
        if rows:
            result.append((name, len(rows), total))
    return result


def kill(name: str) -> None:
    subprocess.run(["taskkill", "/IM", f"{name}.exe", "/F"], capture_output=True)
    time.sleep(1.5)


def show(rows: list[tuple[str, int, float]]) -> None:
    if not rows:
        print("  很干净，没有腾讯系进程在跑。")
        return
    for name, count, mb in rows:
        print(f"  {name:<12} {count:>3} 个   {mb:>7.0f} MB")


def main() -> None:
    print("=" * 46)
    print(" 腾讯系后台进程")
    print("=" * 46)
    show(snapshot())

    print()
    print("  1) 退出微信（整只关掉，内嵌浏览器会一起没）")
    print("  2) 退出 QQ（整只关掉）")
    print("  3) 只清微信内嵌页面（注意：微信会立刻重新拉起）")
    print("  0) 退出")
    print()

    choice = input("  选一个：").strip()

    if choice == "1":
        kill("WeChatAppEx")
        kill("WeChat")
        kill("Weixin")
        print("\n  微信已退出。")
    elif choice == "2":
        kill("QQ")
        kill("QQKit")
        print("\n  QQ 已退出。")
    elif choice == "3":
        kill("WeChatAppEx")
        print("\n  清了一次。几秒后微信大概率会重新拉起它们，这就是为什么杀不干净。")
    else:
        print("\n  什么都没做。")
        return

    time.sleep(1)
    print("\n  现在：")
    show(snapshot())


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        try:
            input("\n  按回车关闭…")
        except (EOFError, KeyboardInterrupt):
            pass

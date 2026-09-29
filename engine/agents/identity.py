"""打印 Agent 的 GitHub 身份（checks.toml [identity]），供 bin/as-agent 等 shell 入口读取。

    bin/harness identity        输出一行：<login> <email>
"""

from __future__ import annotations

import sys

from engine.core.common import ConfigError


def main(argv: list[str] | None = None) -> int:
    from engine.agents.dispatch import agent_identity

    try:
        login, email = agent_identity()
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"{login} {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

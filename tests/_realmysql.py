"""要**真 MySQL** 的测试从这里拿门。

`ANIMA_TEST_MYSQL` 指向一个连得上的实例时才跑(形如 `127.0.0.1:3306`,或
`unix:///tmp/mysql.sock`),本机没有就整体 skip:

```bash
docker run -d --rm --name tmp-mysql -p 127.0.0.1:13499:3306 \
    -e MYSQL_ALLOW_EMPTY_PASSWORD=yes -e MYSQL_DATABASE=anima_test mysql:8.4
ANIMA_TEST_MYSQL=127.0.0.1:13499 python -m pytest -q
```

**为什么值得有这么一个模块**:抄第二份 `_connect()` 出去,两个文件就对"怎么连"
各持一份猜测,而这两份**只在有 MySQL 的机器上才会同时跑** —— 也就是最不常跑的
那种机器上。fakeredis 那一侧已经吃过一次同类的账:替身全绿,真路上的
`MySQLChatStore.__slots__` 是坏的。
"""
from __future__ import annotations

import importlib.util
import os

import pytest

#: 形如 `127.0.0.1:13499` 或 `unix:///tmp/mysql.sock`;没有就不跑。
DSN = os.environ.get("ANIMA_TEST_MYSQL")

requires_mysql = pytest.mark.skipif(
    not DSN or importlib.util.find_spec("pymysql") is None,
    reason="没有 ANIMA_TEST_MYSQL(形如 127.0.0.1:3306 或 unix:///tmp/my.sock),"
           "或者没装 pymysql",
)


def connect(database: str = "anima_test"):
    """一条新连接。**开 World 的测试把这个函数本身传进 `mysql=`,别传连接。**

    `pymysql` 的 threadsafety 是 1 而引擎有线程池:共用一条连接会让协议帧交叉,
    炸出来的是 `read of closed file` 这种离原因很远、看不出是并发的报错,
    而且不是必现。引擎认工厂就是为了不让调用方踩这个坑。
    """
    import pymysql

    if DSN.startswith("unix://"):
        return pymysql.connect(
            unix_socket=DSN[len("unix://"):], user="root",
            database=database, charset="utf8mb4",
        )
    host, _, port = DSN.partition(":")
    return pymysql.connect(
        host=host, port=int(port or 3306), user="root",
        database=database, charset="utf8mb4",
    )


#: 一个世界在 MySQL 上占的四张表(`{world_id}_` 前缀)。
WORLD_TABLES = ("events", "memories", "conversations", "messages")


def drop_world_tables(conn, prefix: str) -> None:
    """把一个前缀下的四张表删掉 —— 测试之间不许**串台**。

    留着上一次的行,下一次那个测试数出来的条数就不是它自己造的那些,
    而它红的时候会指着一段与它无关的转录。

    ⚠️ **先把 `lock_wait_timeout` 压到 5 秒**:`DROP TABLE` 要元数据锁,而它的默认
    等待是 `31536000` 秒(**一年**)—— 只要还有一条连接攥着这几张表(引擎是
    `ThreadLocalConnection`,`world.close()` 按设计只关**本线程**那条,线程池里那些
    等进程退出),这一句就会挂住不动,而挂一年和挂死没有区别:测试跑不完,
    也不会告诉你为什么。5 秒之后它会**报错**,而报错是查得出来的。
    所以调用顺序也有讲究:**在开世界之前删,别在用完之后删。**
    """
    with conn.cursor() as cur:
        cur.execute("SET SESSION lock_wait_timeout = 5")
        for table in WORLD_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS `{prefix}{table}`")
    conn.commit()

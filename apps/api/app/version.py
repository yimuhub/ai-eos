"""后端发布版本号——仓库内版本字符串的唯一来源。

之前 pyproject / main.py / mDNS 广播 / 三端 package.json 各写一份，
实测已经漂移成 5.9.0 / 5.4 / 0.1.0 三个不同值，"看版本号判断装了哪版"完全失效。
这里收成一个常量，其余位置从这里取；跨端一致性由
tests/test_code_quality.py::test_version_strings_are_consistent 守住。
"""

__version__ = "5.16.1"

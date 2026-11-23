# Android 闪耀耗尽存档恢复

## 原始输入

> 耗尽堆消失的bug好像还存在 比如这里至少耗尽堆本来应该有闪耀打击的（ 不确定是不是因为游戏闪退之后耗尽堆就消失了
> 这是用户反馈，你来结合后台的日志等分析处理下 [$worktree-task]($worktree-task)

> [附件 1：战斗内牌组截图，随后更正]

> 是这张
>
> [附件 2：第 49 层结束的历史截图]

> 都没有了

> 关闭游戏重新打开呢

> 这个用户是玩手机端的，你可以用 ssh -p 2222 <android-test-ssh-endpoint> 你可以用这个安卓机器进行安卓测试

> [附件 3：两份 Android 游戏日志的压缩包]

> 合并两项并清理工作树与任务分支，归档验证证据

> 简明扼要的说明下问题，原因和解决方案

> 先简明扼要的说明下问题，原因和解决方案

用户提供的仓库约定见 `AGENTS.md`。原分支为 `main`，起点 `69fc215`，工作树在 `../worktrees/STS2_ShoujoKagekiCore/shine-exhaust-resume`。原检出已有 `docs/error-watch/README.md` 修改和 `docs/待修复问题.md` 未跟踪文件。桌面诊断记录位于 <Karen 仓库> 的同名任务文档。

## 计划

由 Android 日志定位 BaseLib 初始化扩展存档字段失败的影响。以真实存档路径建立回归，保留原子落盘、当前 JSON 格式、旧字段和联机行为。使用提供的隔离 Android 环境验证重启恢复。

## 根因与改动

- 玩家手机端日志加载 BaseLib 3.4.8、Core 1.9.7 Beta 和 v0.113.0。BaseLib 的 `ExtendedSavePatches.AddContext` 在注册扩展 JSON 时即因 Android 运行时缺少 `JsonPropertyInfoValues<T>.set_IsProperty` 报 `MissingMethodException`。常规玩法继续加载，玩家扩展字段未注册到原生 JSON 序列化器。
- 同一份手机日志在《闪耀打击》打出后报告耗尽堆 0→1，后面多次正常写出 `current_run.save`。下一次续玩初始化耗尽堆为 0。日志还能读取与截图开局时间吻合的历史条目，但附件本身没有原局存档或原生致命闪退调用栈。
- Core 在原生对局保存作用域内保留各玩家已序列化的快照。写盘时，如果 BaseLib 生成的玩家 JSON 缺少 Core 项，按玩家 ID 合入现有 `save_dict_String` 键。现行检查点、其他模组属性和原生原子写入继续沿用，读取则将磁盘中已有的快照绑定回对应玩家。旧 `extra_fields` 迁移路径继续适用。

## 定向回归

- 游戏 v0.113.0，工坊 Core 2.0.2 基线。在隔离探针内移除 BaseLib 的玩家 JSON 元数据扩展后，存档没有 Core 快照。永久牌已移除，重启断言报告耗尽堆应有 1 张而实际为空。
- 使用同一探针运行候选 Core，连续两轮存盘、关闭进程、启动新进程和续玩均通过。磁盘、永久牌组、闪耀预算、耗尽牌的成长、规则快照和可见耗尽堆一致。缺少 BaseLib 玩家 JSON 扩展时读取旧 Karen 字段通过，正常 BaseLib 元数据下现行格式往返也通过。
- Stable v0.107.1、Main v0.111.1、Beta v0.113.0 Runtime 定向构建均为零警告、零错误。目录 `out/tmp/shine-exhaust-resume/` 含隔离证据。候选 Beta Runtime SHA-256 为 `f0336ec7d2ef26fc3c974e31b86c0fbbd4b73f26b7853b8bd316e13e1763b10f`。

## Android 设备结果

使用用户提供的 Redroid 14 隔离测试机。系统为 x86_64，经 Native Bridge 转译 ARM64。成功安装 Android 启动器，导入匹配的 v0.113.0 本体和旧包测试模组，创建隔离配置。

- 启动器 v0.1.13 的默认启动在 AAudio 回调触发 `SIGABRT`。启用启动器声音兼容模式后，OpenGL 与 Vulkan 都转为在 `libndk_translation.so` 的 `RunKernelSyscall` 路径原生退出。
- 关闭全部 Mod 后，v0.1.13 仍在加载 Mod 和达到主菜单前发生相同原生退出。降级到同样支持 v0.113.0 的 v0.1.12 后也未通过启动。未在该机取得 Android Gameplay、存盘或续玩结果。已完成的红绿对照来自 Windows 隔离进程中的同一原生存档路径。
- 修复保留同一快照格式。老存档已经缺少的耗尽记录无法从空快照还原。玩家原局截图未提供存盘时的 JSON，当前手机日志也未给出原始闪退堆栈。

启动器版本依据见 [上游发布记录](https://github.com/ModinMobileSTS/Sts2MobileLauncher/releases)。

## 验证归档

用户已确认合并两个任务提交，保留验证证据并清理任务工作树与分支。测试日志、结果、截图和快照已复制到原仓库忽略目录 `artifacts/task-deliverables/shine-exhaust-resume/`，文件摘要见 `SHA256SUMS.json`。

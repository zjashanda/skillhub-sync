# 仓库清单（第一阶段产物）

本文件是 NOAH-48 门禁所需的「仓库清单」阶段产物：说明 skillhub-sync 本身要发布到哪里、它声明同步哪些上游仓库，以及哪些候选仓库在本阶段**不**纳入并给出理由。

## 1. 本仓库

| 项 | 值 |
|---|---|
| 仓库名 | `skillhub-sync` |
| 归属 | `zjashanda`（GitHub） |
| 可见性 | public |
| 默认分支 | `main` |
| 内容 | `skill_sync.py`、Windows/Linux bootstrap、manifest/profile、单测、文档、敏感信息扫描工具 |
| 首次推送 | **未执行**，等待门禁批准 |

## 2. 声明同步的上游 canonical 仓库（10 个，均为 public）

全部通过 GitHub API 实测存在，默认分支 `main`。

| Skill | 仓库 | 大小 | 根 SKILL.md | 备注 |
|---|---|---|---|---|
| venusahisiburner | `zjashanda/venusahisiburner` | 52KB | 有 | 烧录唯一真源（替代已退役的 tcl-device-autoburn） |
| Polaris | `zjashanda/Polaris` | 24.9MB | 有 | 真机语音验证框架 |
| lstest | `zjashanda/lstest` | 718KB | 有 | 公共测试库 |
| listenai-play | `zjashanda/listenai-play` | 238KB | 有 | 播放/采集入口 + 安装脚本唯一来源 |
| ark-dispatch | `zjashanda/ark-dispatch` | 125KB | 有 | 只做决策不执行的 plan.v1 |
| ark-kb | `zjashanda/ark-kb` | 510KB | **无** | 知识库仓库，manifest 中标记 `require_skill_md: false` |
| mars-belt | `zjashanda/mars-belt` | 16.4MB | 有 | MarsPlatform 打包/烧录/验证 |
| usb-tree | `zjashanda/usb-tree` | 261KB | 有 | USB 拓扑枚举 |
| clearusb | `zjashanda/clearusb` | 21KB | 有 | USB 复位清理 |
| skill-git | `zjashanda/skill-git` | 63KB | 有 | Skill 生成/校验/发布 |

## 3. 明确不纳入（disabled）

| 名称 | 原因 |
|---|---|
| ark-env | 无独立 git 仓库。环境变量真源是 `/srv/storage/ark/env/env.json`，经 HTTP 只读分发（`ark-env.ps1` / `ark-env.sh`）。待 NOAH-49 `ark-node` 建立仓库后再纳入。 |
| ark-mesh | 无独立 git 仓库。节点取件能力目前只是单个 `ark_fetch.py`。待 NOAH-49 `ark-node` 建立仓库后再纳入。 |

这两项在 `manifests/canonical.json` 的 `disabled` 数组中保留条目与理由，**不**出现在任何 profile 的 include 列表里，因此不会被同步，也不会被误判为 ABSENT。

## 4. 本阶段不触碰的仓库

按 NOAH-45 决议：旧仓库在替代验证通过前保持原样，本阶段不做任何归档、冻结、删除、改名，也不在旧仓库里建迁移分支。归档候选（4 个）由 NOAH-47（ark-archive）单独处理，且只允许「提议归档 / 打 tag 冻结」，删除需单独授权。

## 5. 节点与 profile 对应

| 节点 | 平台 | profile | 同步范围 |
|---|---|---|---|
| bszT14 | Linux | `bsz-t14` | skill-git、ark-kb、ark-dispatch、lstest |
| Windows 台架（win005 等） | Windows | `win-bench` | venusahisiburner、lstest、listenai-play、Polaris、usb-tree、clearusb、skill-git |

其余节点从 `manifests/profiles/template.json` 复制后按需裁剪；不允许按 tag 全量同步。

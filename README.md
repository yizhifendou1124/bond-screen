# bond-screen

面向中国公募债券基金的纯债筛选技能与命令行工具。预制默认目标固定为 **400030 东方添益债券**，可临时指定其他目标；不包含个人持仓金额、加仓计划、固定候选名单或上次筛选结果。

> 一键执行的是“刷新市场数据 → 数值预筛 → 管理人报告核查 → 当前申购核查 → 筛选和导出”，不是联网模型自由猜测。公开数据源不可用或证据无法解析时，停止或列待核实，绝不拿旧结果假装最新。它不是交易工具，也不保证收益、数据源永远可用或穷尽所有无法核实的基金。

## 快速使用

需要 Python 3.10+、Git 和网络；Linux若缺少 `venv`，请先安装系统的 `python3-venv`。首次运行会建立仓库内 `.venv` 并通过 pip 安装固定已验证版本的 `requests`、`pypdf` 及跨平台时区库 `tzdata`。无需基金账户、GitHub token 或模型 API key。

```bash
git clone https://github.com/yizhifendou1124/bond-screen.git
cd bond-screen
bash run.sh
```

后续每次只需一条命令，默认400030、重新取最新市场数据：

```bash
bash run.sh
bash run.sh --target-code 015736
bash run.sh --top-lists --workers 4 --output-dir ./results
```

Windows PowerShell：

```powershell
git clone https://github.com/yizhifendou1124/bond-screen.git
cd bond-screen
powershell -File .\run.ps1
powershell -File .\run.ps1 --target-code 015736 --top-lists
```

如果所在机器的策略禁止执行脚本，可手动建立虚拟环境、安装 `requirements.txt`，再执行 `python scripts/run_market.py`；不要为了运行本项目永久降低系统执行策略。Windows启动器尚需在Windows实际环境验证。

第一次全市场执行可能需数十分钟或更久，取决于数千份额数据、管理人PDF及源站限流。默认6个并发，最大8；请求在同一主机间共享节流，遇到429/503/514会全局冷却、降低速率并尊重Retry-After，最多尝试5次。遇到限流建议 `--workers 2`，不要提高请求强度或绕过限制。

## 安装为 Codex skill

在自己的工作项目内安装，不要覆盖已有同名技能：

```bash
mkdir -p .agents/skills
git clone https://github.com/yizhifendou1124/bond-screen.git .agents/skills/bond-screen
```

重新打开对应项目的 Codex 后使用：

```text
$bond-screen 筛选符合条件的纯债基金
$bond-screen 以015736为目标基金筛选
$bond-screen 生成今年、近半年、近3个月各最多5只
```

该技能运行同一套命令行流程；模型负责解释报告及待核实项，不能把未经核查的数据改成合格。实际发现目录以所用 Codex 版本的技能说明为准。

技能目录只读时，将运行环境与结果放在项目可写目录，也可不经模型直接运行：

```bash
BOND_SCREEN_VENV="$PWD/.bond-screen-venv" bash .agents/skills/bond-screen/run.sh
```

更新仓库或已安装技能：

```bash
git pull --ff-only
```

## 核心规则

- 每次刷新全市场债基目录、排名分页、缺失阶段收益及同基金身份；不从历史名单开始。
- 候选总净资产 **≥1亿元**，全部份额合计，最新管理人报告；当前在天天基金渠道可申购，正额度限购保留并显示限额。
- 合同与完整披露持仓确认纯债；合同允许非纯债资产、实质未投资者可条件纳入，但缺少组合/交易证据只列待核实，不给004042永久白名单。
- 从2020年起所有完整年度不能有负收益；实际0%不覆盖；缺失只能由核实的同基金其他份额补全，不借用其他基金或同类均值。
- 异常年份：该年收益高于**其他完整年度算术平均**至少15个百分点时剔除，边界含等号；已确认非经常性净值异常同样剔除，疑点待核实。2020年前不考虑。
- R1：同一目标2020—2025每年都严格高于候选则剔除。R2：今年落后目标≥0.5个百分点，同时候选各历史年度最多领先≤0.2个百分点则剔除。
- R3：今年落后目标≥2个百分点直接剔除，长期领先也不能豁免。
- 三年和五年均不低于目标，或至少一项不低于目标，可进入相应分类；两项均低只在今年不低于目标0.3个百分点且至少一个历史年度不低于目标时例外保留，并仍遵守所有剔除项。
- 三五年采用最近3/5个**完整日历年度**复利年化；2026年分别是2023—2025、2021—2025。在线流程随日历年更新年化窗口；R1/R2历史比较集合仍保持预设2020—2025，不悄悄改规则。
- 分类顺序固定：两项均优 → 单项不低 → 近期例外。每类按**原始今年收益降序**，同收益代码升序；同基金去重；不硬凑数量。

完整定义以 `SKILL.md`、`references/defaults.json` 和 `references/data-contract.md` 为准。直接调用 `scripts/screen_funds.py INPUT.json` 只是离线计算，不表示联网完成全市场筛选。

## 结果与失败

输出到调用者当前目录的 `bond-screen-results/<时间戳>_market/`，也可用 `--output-dir` 指定：

|文件|内容|
|---|---|
|`report.md`|分类表、目标、日期、限购、待核实和覆盖风险|
|`retained_funds.csv` / `.json`|同基金去重后的正式名单，三类和今年降序一致|
|`screen_input.json` / `screen_output.json`|可复核输入、精确规则结果、补全与剔除见证|
|`coverage.json` / `market_audit.json`|目录数量、阶段预筛、未解决错误和覆盖缺口|
|`catalog.json` / `identities.json` / `annuals.json` / `fresh_funds.json`|本次采集的目录、身份、年度请求状态及预筛数据；只作审计，不作为下次候选输入|
|`family_reviews.json` / `source_manifest.json` / `evidence/`|报告依据、年度核对、公告及请求时间、原文和哈希|
|`failure.json`|关键失败或用户中断的诊断，不是成功结果|
|`top_lists.json`|仅 `--top-lists` 时输出三个近期榜单|

输出根目录的 `latest.json` 只在目标有效且本次导出完成后更新；关键失败时不回退读取它。退出码 `0`=完成，`3`=完成但有覆盖缺口，`2`=关键失败，`130`=中断。退出码3的正式名单仍通过硬门槛，但不能宣称全市场完全穷尽；检查覆盖文件。

网络响应、JSON、历史收益、身份、交易状态和公告目录每次刷新；最新完整报告、招募说明书及实际持仓例外的最近完整期报告重新下载。历史年报按公告编号缓存，复用前实时刷新公告目录并校验本地PDF的已记录哈希；不用上次候选名单或历史JSON作为新一轮输入。源站若静默替换同编号的历史附件，缓存不保证立即识别；当前年度行情与报告收益不一致时列待核实，不放行。

## 测试与维护

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/run_market.py --help
```

仓库 CI 使用离线合成数据，不将实时第三方服务可用性当成确定通过的单元测试。真实全市场运行应检查报告日期、覆盖和目标有效性。数据页面/接口结构发生变化时会报错或观察，需要维护解析器，而不是放松硬门槛。

数据源为天天基金/东方财富公开页面与公开接口、管理人公开披露的完整报告PDF。接口不是本项目控制的稳定商业API，不承诺SLA；尊重源站服务条款和访问限制，不绕过认证、付费墙或限流。基金收益不含个人申赎费用，公开持仓有披露滞后；本工具不保本、不自动下单。

## 许可

MIT，沿用本仓库已有 `LICENSE`。许可适用于项目代码，不授予第三方行情、公告或PDF的再分发权；生成的市场快照和缓存默认不提交到仓库。

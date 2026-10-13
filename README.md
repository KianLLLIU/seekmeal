# SeekMeal · 智能饮食健康助手

面向中文用户的本地饮食记录与营养管理工具：用可追溯的确定性公式计算目标，用自然语言记录饮食，并给出营养评估、菜谱推荐和一日排餐。

> **免责声明：本工具仅用于饮食记录与营养参考，不构成医疗建议，不能替代医生诊疗。**

## 功能清单

- 身体档案：计算 BMI、BMR、TDEE、目标热量及蛋白质/脂肪/碳水/膳食纤维目标
- 中文饮食记录：识别“一碗米饭、鸡胸肉100g”等自然语言和常见份量
- 营养评估：汇总热量与宏量营养素，显示达标状态及规则化风险提示
- 菜谱推荐：以无第三方依赖的字符 bigram TF-IDF 结合营养缺口进行排序
- 一日排餐：生成早餐、午餐、晚餐及合并后的采购清单
- 体重趋势：提供 EMA 与一维卡尔曼滤波趋势
- 多种入口：中文命令行、零构建网页和 FastAPI 接口
- Windows 一键启动：双击 `start.bat` 即可选择常用功能

## 重要数据说明

**内置的 100 条食物与 22 道菜谱仅为演示数据，不完整，也未覆盖品牌差异、烹饪损耗、生熟重换算和个体差异，不可用于医疗、商业或任何实际健康决策。** 正式使用前应替换为获合法授权且经过专业校验的完整数据源。

## 目录结构

```text
SeekMeal/
├── README.md
├── requirements.txt
├── .gitignore
├── start.bat                 # Windows 菜单启动器
├── start_web.bat             # 网页模式快捷启动器
├── diet_assistant/
│   ├── __init__.py
│   ├── formulas.py           # 代谢与营养公式
│   ├── food_db.py            # 中文食物库与自然语言解析
│   ├── analyzer.py           # 营养核算、达标评估、风险规则
│   ├── recommend.py          # TF-IDF 菜谱推荐
│   ├── planner.py            # 三餐排餐与采购清单
│   ├── cli.py                # 中文命令行
│   ├── api.py                # FastAPI 接口
│   ├── data/
│   │   ├── foods_zh.json     # 100 条演示食物
│   │   └── recipes_zh.json   # 22 道演示菜谱
│   └── tests/test_core.py
├── web/index.html            # 单文件原生网页
└── docs/技术方案.html
```

## 快速开始

### 1. 环境准备

需要 Python 3.10 或更高版本。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
```

营养计算、食物库、推荐、排餐和命令行仅使用 Python 标准库；只有 API 与网页模式需要安装 `requirements.txt` 中的依赖。

### 2. Windows 一键启动

双击项目根目录的 `start.bat`，可选择：

1. 命令行模式
2. 启动网页（自动启动 API 并打开浏览器）
3. 运行单元测试
4. 打开技术方案文档
5. 退出

也可双击 `start_web.bat` 直接进入网页模式。

启动器寻找 Python 时会**逐个实测 `import uvicorn, fastapi`**，按以下顺序取第一个真正可用的解释器：

1. `项目目录\.venv\Scripts\python.exe`
2. `项目目录\..\.venv\Scripts\python.exe`
3. 交付自带解释器 `%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe`
4. `py -3`
5. `python`

只「存在」但没装依赖的解释器会被跳过，不会再出现「明明装了 Python 却提示缺 uvicorn」的情况。若以上全都不满足，脚本会打印确切的安装命令后退出。

### 3. 命令行模式

```bash
python -m diet_assistant.cli
```

首次进入后按提示建立档案。自然语言记录示例：

```text
一碗米饭、150g鸡胸肉、两个鸡蛋
```

命令行会把本地状态保存到项目根目录的 `diet_state.json`；该文件已被 `.gitignore` 排除。

### 4. 网页模式

推荐双击 `start_web.bat`，或手动执行：

```bash
python -m uvicorn diet_assistant.api:app --host 127.0.0.1 --port 8000
```

然后访问 <http://127.0.0.1:8000>。页面由 API 自身托管，与接口同源，前端会自动使用相对路径请求，无需额外配置。

页面也可以直接双击 `web/index.html` 打开（此时请求回退到 `http://127.0.0.1:8000`），但本地 API 仍需运行。网页会显示服务连接状态，并在服务未启动时给出中文提示。

部署到公网时同理：只要页面与 API 由同一域名托管，前端无需改动任何地址配置。

#### 把页面接到别的 API（跨域）

如果网页和 API **不在同一个域名**（例如页面放 GitHub Pages、API 在别处），需要两步：

**前端**：页面底部「页面请求地址」旁的「设置 API 地址」可以指定任意 API 基址，保存在本机浏览器。也可用查询参数临时指定：

```text
https://你的页面地址/index.html?api=https://你的API域名
```

留空即回到同源默认值。不写协议时会自动补全：`127.0.0.1:8000` → `http://`，其他 → `https://`。

**后端**：浏览器的跨域请求需要 API 放行来源。默认只允许本机与 `file://`，要放开需显式声明：

```bash
# Windows PowerShell
$env:SEEKMEAL_ALLOWED_ORIGINS="https://your-name.github.io"
python -m uvicorn diet_assistant.api:app --host 0.0.0.0 --port 8000
```

```bash
# macOS / Linux
SEEKMEAL_ALLOWED_ORIGINS="https://your-name.github.io" \
  python -m uvicorn diet_assistant.api:app --host 0.0.0.0 --port 8000
```

- 多个来源用逗号分隔；末尾斜杠会自动去掉；只做整串精确匹配。
- 值写 `*` 表示放行全部来源，**只建议本地调试用**。
- 公共部署建议直接让 API 托管页面（即上面的默认方式），避免跨域与来源维护。

使用顺序：

1. 在“身体档案”中计算并保存目标；
2. 在“饮食分析”中提交中文饮食文本；
3. 查看营养汇总和达标评估，或生成一日排餐与采购清单。

### 5. API 模式

```bash
python -m uvicorn diet_assistant.api:app --reload --port 8000
```

- Swagger 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>
- 默认仅监听本机回环地址，不对局域网或公网开放

### 6. 运行测试

```bash
python -m unittest discover -s diet_assistant/tests -t .
```

当前测试集包含 27 项测试，覆盖 BMR/TDEE 已知值、宏量自洽性、中文食物名与份量解析、营养汇总、推荐排序和排餐份量缩放。

## 公式与数据来源

| 能力 | 实现 | 参考来源 |
|---|---|---|
| BMI 与分级 | `体重kg / 身高m²`，中国标准切点 18.5 / 24 / 28 | WHO、中国标准 |
| BMR | Mifflin-St Jeor（默认）、Harris-Benedict（备选）、Katch-McArdle（提供体脂率时） | 各公式原始文献，详见函数 docstring |
| TDEE | `BMR × 活动系数`（1.2 / 1.375 / 1.55 / 1.725 / 1.9） | 2024 Compendium |
| 目标热量 | 由 TDEE 与目标体重变化推导，带安全下限（男 1500 / 女 1200 kcal） | 1 kg 体脂约 7700 kcal |
| 宏量分配 | 蛋白质、脂肪优先满足克重范围，碳水补足其余热量 | WHO TRS 916 |
| 膳食纤维 | 14 g / 1000 kcal | 中国 DRIs |
| 血糖负荷 | `GL = GI × 碳水g / 100` | 通用定义 |
| 钠上限 | 2000 mg/日（约 5 g 盐） | 中国居民膳食指南 |
| 体重趋势 | EMA（α=0.15）与一维卡尔曼滤波 | 通用信号处理方法 |

**设计原则：所有数值均由确定性代码计算。** 推荐算法只参与排序，不改写营养数据。

内置 `foods_zh.json` 为 100 条手工整理的学习演示数据，部分字段参考《中国食物成分表》常见值；纸质版/电子版具有版权，商用应取得授权。Open Food Facts 与 USDA FoodData Central 可作为 CC0 数据替代来源，但仍需专业校验与字段清洗。

## 参考的开源项目

以下引用保留原项目设计调研内容。使用外部数据或代码前，请自行核对其最新许可证。

### 直接借鉴

| 项目 | 成熟度 / 价值 | 本项目的借鉴方式 |
|---|---|---|
| [Sanotsu/china-food-composition-data](https://github.com/Sanotsu/china-food-composition-data) | 中文食物成分数据整理 | 《中国食物成分表（第 6 版）》OCR 转 JSON / SQLite，并附 GI 数据；本项目仅内置 100 条演示精简数据，正式使用需更换合法完整数据源 |
| [simonoppowa/OpenNutriTracker](https://github.com/simonoppowa/OpenNutriTracker) | 公式来源清晰的营养追踪应用 | 借鉴公式溯源方法：热量、BMI、宏量与活动消耗均标明出处 |
| [slimane-lakehal/calorie_tracker](https://github.com/slimane-lakehal/calorie_tracker) | Python CLI，模块结构完整 | 借鉴 BMR/TDEE、食物库、饮食记录与体重追踪的模块划分 |
| [CheeChoong14/Food-Recipe-Recommender-System](https://github.com/CheeChoong14/Food-Recipe-Recommender-System) | FastAPI + React 菜谱推荐 | 借鉴 TF-IDF + cosine 内容过滤思路，并以无依赖字符 bigram 实现核心逻辑 |
| [Anduin2017/HowToCook](https://github.com/Anduin2017/HowToCook) | 大规模中文菜谱语料 | 可作为后续菜谱冷启动和扩充的参考；本版 22 道菜为演示数据 |

### 后续版本调研

| 项目 | 用途 |
|---|---|
| [Decade-qiu/CookHero](https://github.com/Decade-qiu/CookHero) | FastAPI + React + PostgreSQL + Milvus 的全栈与 RAG 架构参考 |
| [RitamRixx/NutriGen_AI](https://github.com/RitamRixx/NutriGen_AI) | LangGraph 画像收集、指标计算、RAG 与生成餐单流程 |
| [WUT-IDEA/MealRec](https://github.com/WUT-IDEA/MealRec) | 将“一餐多道菜”建模为 bundle 推荐与排餐优化 |
| [AmirrrezaZ/dish2calories](https://github.com/AmirrrezaZ/dish2calories) | Nutrition5k 拍照估卡研究参考 |
| [txxxxz/LightTable](https://github.com/txxxxz/LightTable) | 冰箱库存驱动的推荐场景参考 |

## 替换食物数据

`FoodDB` 支持传入兼容 schema 的 JSON：

```python
from diet_assistant import FoodDB

db = FoodDB.load("path/to/validated_foods.json")
```

最小结构示例：

```json
{
  "foods": [
    {
      "id": "rice",
      "name": "米饭",
      "alias": ["白米饭"],
      "cat": "主食",
      "kcal": 116,
      "protein": 2.6,
      "fat": 0.3,
      "carb": 25.9,
      "fiber": 0.3,
      "sodium": 2.5,
      "gi": 83,
      "portions": {"1碗": 150}
    }
  ]
}
```

`portions` 是中文交互的重要字段，它让“一碗米饭”能换算成约定克重。扩展数据时应明确份量口径并校验来源。

## 已知限制

1. **演示数据有限。** 100 条食物和 22 道菜谱不能代表完整食物成分数据，严禁据此作实际健康决策。
2. **排餐为贪心算法。** 当前在 0.5–2.0 倍范围内缩放份量，热量通常较接近目标，但脂肪或碳水仍可能明显偏离；生产版本宜使用整数规划。
3. **营养值未覆盖烹饪变化。** 未完整考虑烹饪损耗、吸油量、生熟重换算和调味品误差。
4. **自然语言解析范围有限。** 可识别内置名称、别名、克重和常见量词，但不是通用语言模型。
5. **API 状态仅存内存。** 服务重启后网页档案与饮食记录会清空；命令行状态单独保存在本地 JSON，两者不互通。
6. **网页面向本机单用户演示。** 没有账号、鉴权、数据库、隐私隔离、HTTPS 或公网部署加固。
7. **无图像识别与协同过滤。** 拍照估卡和多人行为推荐均不在当前版本范围内。
8. **不适用于特殊人群自行决策。** 孕妇、儿童、慢性病患者和有进食障碍史的人群应遵医嘱。

## 免责声明

- 本工具不提供诊断、治疗建议或用药指导。
- 目标热量包含基础安全下限，但不等同于个体化医疗评估。
- 对数据、算法或界面结果有疑问时，应咨询注册营养师或医生。

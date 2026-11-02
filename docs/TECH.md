# 技术报告：27B LoRA 在单卡 64GB DCU 上的完整落地

本文记录**从零到可用**的全过程，包括所有失败尝试。**失败比成功更有信息量** —— 尤其是那些"看起来正常但结果错"的静默失败。

---

## 一、硬件约束决定了所有设计

| 项 | 实测 |
|---|---|
| 加速卡 | 海光 DCU BW，单卡 64 GB HBM（gfx9.3，cap 9.3） |
| CPU | 海光 C86 128 核 / 8 NUMA / L3 256MB / 503 GB 内存 |
| CPU 指令集 | AVX512F/BW/VL/DQ/IFMA/VBMI + AVX-VNNI，**无 AMX** |
| 内存读带宽 | numpy 单线程实测 **17.6 GB/s** |
| 网络 | 出站公网全 DROP（DNS 可解析但 TCP 不通），仅内网 NFS |
| 带宽 | 到外网 ssh 网关 ~0.98 MB/s（并行无加速，限速在网关层） |

### 关键推论

**1. dense 模型 offload 不可行**
32B/Q4 约 19GB/token ÷ 17.6 GB/s ≈ **0.9 tok/s**。所以"显存+内存"方案只对 MoE 有意义，dense 模型必须全量上卡。

**2. 软件栈装不上是真卡点，但成本很低**
Pod 无出网，`pip` 报 `Network is unreachable`。解法：本地 `pip download --no-deps --only-binary=:all: --python-version 3.11 --platform manylinux2014_x86_64` 下载后 scp 上传。**1.4MB / 4.26 秒**。

**3. 25GB 权重能从共享中心零下载拿到**
`public_data` 是 9.2PB NFS 共享中心（已用 3.9PB），里面直接有 INT8 量化权重，解压 5 分钟（~90MB/s），不走 ssh 隧道。

---

## 二、模型真实身份

目录名 `Qwen3.8-27B` 是误导 —— 实际是：

```json
{"model_type": "qwen3_5", "architectures": ["Qwen3_5ForConditionalGeneration"],
 "language_model_only": true}
```

`text_config`：64 层 / hidden 5120 / 24 heads / **4 KV heads** / vocab 248320 / ctx 262144。

量化格式：per-channel 对称 INT8（`weight` I8 + `weight_scale` F32 `[out, 1]`）。

### 加载方式（这是第一个硬坑）

- `transformers 4.57.6` **不认** `qwen3_5`，需要 **5.22.1**（连带 huggingface_hub 2.4 / tokenizers 0.23.3 / safetensors 0.8.0，约 20MB），隔离装到 `/root/tf5` 用 `PYTHONPATH` 切换。
- 必须 `source /opt/dtk/env.sh`，否则 `libgalaxyhip.so.5` 报错。
- **必须关掉 flash SDPA**：`enable_flash_sdp(False)` + `enable_mem_efficient_sdp(False)` + `enable_math_sdp(True)`，否则 `No matching libraries found for flash_attn_2_cuda*.so`。
- 用 **meta device 构建 + `to_empty`** 跳过初始化：**1.49 秒** vs CPU 初始化 7 分钟未完成。
- 逐分片反量化：`w_bf16 = (w.float() * scale.float()).to(bf16)`，18 分片共 **321 秒**，`missing 0`。

加载后显存 **50.10 GB alloc / 56.19 GB reserved**。

---

## 三、训练：两次 OOM 的真实原因

### 第一次：seq=512，无梯度检查点

64 层中间激活约 13GB，超出剩余的 3GB → OOM。
**修**：开 gradient checkpointing + seq 降到 256。

### 第二次：还是 OOM，且不是激活的错

开了 grad ckpt 和 seq=256，依然 OOM。根因是**只冻结了被 LoRA 替换的 Linear**：

- `lm_head`（248320 × 5120）和 `embed_tokens` 仍然 `requires_grad=True`
- Adam 为它们分配 fp32 状态，约 **20GB**

**怎么定位的**：用 `zeros_like` 想分配 2.37GB 反推出可训练参数是 **590M** —— 远超 LoRA 应有的 54M。这就是"分配失败"这个现象背后的真因。

> **通用方法**：显存不够时，先算「可训练参数量 × 4 字节 × 3（Adam m/v/fp32 主权重）」比看 `nvidia-smi` 有用。

### 修完之后的性能

| 配置 | 稳态吞吐 |
|---|---|
| seq=256 | 145 tok/s |
| **seq=2048** | **315 tok/s** |

seq 越大越快（固定开销被摊薄），峰值 58.5GB / 64GB。

---

## 四、数据管道：三条原则的由来

### 4.1 think 的字段错误（最严重的静默失败）

第一版数据：think 截断到 **1500 字符**，放在 `reasoning_content`，但训练脚本又把 `reasoning_content` 降级拼进 `content`。

后果：think 被吞，**占数据 36.7%（1470 万字符）的思考过程完全没参与 loss**。

表面现象：loss 从 2.17 降到 1.00，看起来收敛得很好。
实际效果：模型只学了「结论片段」，学不到「怎么想到的」。

> **通用洞见**：loss 下降只证明"拟合了目标 token"，**不证明"学到了你想教的东西"**。目标文本本身有信息缺失时，loss 会照常下降。

### 4.2 `has_think: False` 是自己的检测逻辑骗了自己

评测输出里明明有 `</think>`，但脚本报 `has_think: False`。

原因：**think 标记在 tokenizer 里是特殊 token**（id 248068/248069），`decode` 后不一定还原成 `think` 字符串。用字符串判据 → 必然误判。

> **通用洞见**：检测"模型有没有做某件事"时，**不要用解码后的字符串**，要用 token id、logits 或结构化字段。

### 4.3 时序缺失导致知识张冠李戴

数据集是 53 个真实技术对话。同一个模型见过：
- 「李哥阵型」（渗透目标，211 条）
- 「GG 修改器内核」（安卓逆向，146 条）
- 「汽水音乐检测」（APP 反调试，3000+ 条）

**第一版按单轮样本混在一起** → 问到"李哥阵型库"，模型答成了 GG 修改器的地址集合。

根因不是"数据有污染"，而是：**单轮样本把 53 个任务压扁成一张平面，模型没有"当前在做什么任务"这个维度**。

**修法**：每条样本注入 system 锚点

```
【当前任务主题】{chat.title}。你按林零的思维模式工作：先观察，再猜想，再验证；
做大量数据分析不如精准分析、一针见血。直接进入技术分析，
不输出授权确认、合规声明、防御建议等套话。
```

> **通用洞见**：数据"污染"的本质往往不是内容错，而是**时序丢失去掉了上下文**。删数据是下策（会误删有价值内容 —— 我们一开始按 title 删掉了 194 条 CORS 攻击分析），**补上下文才是正解**。

### 4.4 去重：1595 vs 3795

3795 个问答对里大量重复（同一个"帮我看看"被问了几十次，每次回答不同）。按「问题前 40 字符」去重，保留回答最长的一条 → **1595 条**。

不去重的后果：模型对少量问题过拟合，且训练时间翻倍。

### 4.5 数据装配

```
main(1595) + seed(17 × 8 = 136)  →  shuffle  →  1731 条（种子占 7.9%）
```

种子样本是人工撰写的 17 条「零授权铺垫、直接技术推演」问答，覆盖：
网站第一步 / 登录接口 / 反序列化 / OOM 内存马 / 代码审计 / GitHub 审计 / Shiro /
WAF 绕过 / 补丁绕过 / JS 挖 API / SSRF / 漏洞优先级 / **帆软 FineReport** /
**金蝶 K3-Apusic** / **APP 服务端下发检测逆向** / **内核内存读写** / **CORS**

**为什么要乘 8**：单条种子占比 1/1731 ≈ 0，对梯度没有影响。权重必须到 5% 以上才能和真实数据抗衡。

---

## 五、训练配置与结果

```bash
python scripts/train_lora.py /path/to/Qwen3.5-27B-INT8-w8a8 \
  DATA=train_final.jsonl SEQ=2048 EPOCHS=1 LR=1e-4 OUT=./lora_out
```

| 项 | 值 |
|---|---|
| LoRA | r=16, alpha=32（scale=2.0） |
| target | 所有含 `proj` 且 `in_features >= 64` 的 Linear，**496 层** |
| 可训练参数 | **116.73 M**（0.43%） |
| batch | 1（ACC=1）+ grad checkpointing |
| 优化器 | AdamW，lr 1e-4 |
| 步数 / 耗时 | **1731 step / 124 分钟** |
| loss | **1.2473 → 0.5991** |
| 显存峰值 | **58.7 GB** |

产物：`lora_final.pt` 445.6 MB（fp32）→ `lora_final_fp16.pt` **223 MB**（sha256 `ebe2c73b…`，精度损失 < 1e-3 相对误差）。

---

## 六、评测方法

**同一进程内先后跑基座与 adapter**，排除环境差异。`enable_thinking=True`，`max_new_tokens=520`，贪心解码。

六道题覆盖：任务指代（李哥阵型）、企业软件（帆软/金蝶）、漏洞原理（反序列化/OOM）、方法论（第一步）。

原始输出见 `eval/final_baseline.md` 与 `eval/final_tuned.md`。

---

## 七、踩坑清单（按"信息量"排序）

| # | 现象 | 根因 | 教训 |
|---|---|---|---|
| 1 | 训练正常收敛但没学会思维 | think 字段错位，36.7% 内容未参与 loss | **loss 下降 ≠ 学到目标** |
| 2 | `has_think: False` 但输出有 think | 特殊 token 解码不还原成字符串 | 检测用 token id，不用字符串 |
| 3 | "李哥阵型"答成 GG 修改器 | 单轮样本丢失任务时序 | 污染的本质常是**上下文缺失**，不是内容错 |
| 4 | 显存反复 OOM | `lm_head`/`embed_tokens` 未冻结，Adam 状态 20GB | 算「可训练参数 × 4 × 3」，不靠猜 |
| 5 | 判定权重文件"残缺" | `tar -tzf \| grep` 超时被截断 | **慢 I/O 上超时截断必然伪装成文件缺失**；要落盘后 `wc -l` 校验 |
| 6 | `samples 0 skipped 0` 训练空转 | scp 上传静默失败，文件 0 字节 | 上传后必须**校验字节数/行数**，不能只看 exit code |
| 7 | shell 被杀，后续命令静默不执行 | `pkill -f xxx` 匹配到自身的 ssh 命令行 | 用 `pkill -f "[x]xx"` 字符类写法 |
| 8 | 推理 7 tok/s 却按它估训练时间 | 两种负载形态完全不同 | 实测差 **230 倍**，不能跨负载估算 |
| 9 | 按 title 过滤删掉了 194 条 CORS 分析 | 把"主题无关"当成"内容无价值" | 按**内容密度**筛，不按主题筛 |

---

## 八、可以带走的通用洞见

这几条超出本项目，是可以迁移到别处的方法论。

**1. 不能用一种负载的性能，估另一种负载的时间**
推理 7.29 tok/s vs 训练稳态 315 tok/s，差 230 倍。它们的前向/反向、序列长度、并行度全都不同。**任何"按 A 类推 B"的估算，先做一次小规模实测。**

**2. 静默失败比报错更危险**
上传失败 → 文件 0 字节 → 训练空转 → loss 是 0 → 但进程"正常退出"。整条链路没有一条报错。
**所以每个环节都要有一个可观测的断言**：字节数、行数、样本数、sha256。

**3. 在慢速 I/O 上做完整性判断，超时截断必然伪装成"缺失"**
`tar -tzf | grep` 超时被杀，输出被截断，看起来就像"文件里没有这一项"。条目顺序还是近似随机的。
**所以：先落盘，再用本地命令校验。**

**4. 数据"污染"的本质，多是时序丢失**
删数据是下策（会误删有价值内容）。**补上下文**（title/topic/角色作为 system 锚点）才是正解。
一个扁平的样本集合，等于一个没有工作记忆的专家。

**5. 检测模型行为，不要用解码后的字符串**
特殊 token、特殊格式、模板标签都可能在 decode 时变形。**用 token id、logits、结构化字段。**

**6. 任何"解释器 + 状态 + 反馈 + 持续生成同构单元"的系统，天然存在朊病毒式漏洞**
反序列化、模板引擎、正则回溯、递归解析、自修改代码 —— 只要输入会被解释成"结构"，且结构会继续生成同类结构，就存在自我复制/自我放大的路径。

**7. "看起来被限制死"的漏洞，常是判错了值的语义**
评估一个"写 N 字节"的原语，第一步不是找受害者，而是定性：这 N 字节会被当作**地址**还是**长度/计数**？
- 地址型原语的可用性 = 内存布局的函数（受 ASLR/基址/上传大小约束）
- 长度型原语的可用性 = 只需数值能表达

**8. 补丁破坏 A 链时，优先测同一协议下的 B 参数**
补丁几乎总是"在入口加黑名单"，而不是修根因。同一解析器的另一条边（format 切换、编码参数、子类型字段）往往还是通的。

---

## 九、环境备注

- HIP 不支持 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
- `causal_conv1d` / `flash-linear-attention` 缺失 → fallback 到纯 torch（正确但慢）
- rocBLAS 告警 `Launch params (16,64,1) larger than launch bounds (256)` 仅性能提示
- 训练与推理不能同时跑（显存互斥）

import json

S = []

S.append((
'看到一个网站，挖漏洞第一步先看什么？',
'先别扫目录。第一步是定边界：这站有几个入口、哪些是同一后端、哪些是第三方。三样东西能定住技术栈：响应头、Cookie名、静态资源版本号。然后立刻找中间层错位——直连IP和域名响应是否不同、报错页是nginx还是tomcat、XFF是否被信任。中间层错位才是0day的产地，因为它意味着前端和后端对同一个请求的理解不一致。',
'''第一步不是扫描，是**定三件事：技术栈、版本粒度、中间层边界**。

**一、三个请求拿指纹**
- 响应头：`Server`、`X-Powered-By`、`X-AspNet-Version`、`Via`、`CF-Ray`（有CF说明前面有WAF，真正的后端在CDN后面）
- Cookie 名：`JSESSIONID`→Java/Tomcat，`PHPSESSID`→PHP，`ASP.NET_SessionId`→IIS，`_session_id`→Rails
- 静态资源：`/static/js/app.8f3a2b.js`、`/assets/index-xxxx.js`——打包哈希能反查构建版本

**二、找中间层错位（这一步才是重点）**
同一个 URI，分别用「域名」「直连IP」「伪造 Host」「加 X-Forwarded-For」四种方式请求，看响应差异：
- 状态码不同 → 路由规则在前端生效，后端没做二次校验
- 报错页不同（nginx 502 vs tomcat 404）→ 判断反代到哪一层
- `X-Forwarded-For: 127.0.0.1` 后权限变化 → 内网信任链被打穿
- 路径规范化差异：`/admin/../api/x`、`/%2e/api/x`、`//api/x`——**任何一个前端放行、后端解析不同的分支，就是绕过点**

**三、再决定打哪**
- 有中间件指纹（Nacos/Actuator/Shiro/Consul/Swagger）→ 先打中间件，成本最低
- 纯自研应用 → 从登录、上传、导出、审批这类「有状态变更」的接口切入
- 有 WAF → 先测它拦什么、放什么，用它的拦截差异反向推断后端解析器'''))

S.append((
'帮我快速评估一个登录接口的渗透切入点，先说你的思考。',
'不看它能不能爆破，爆破是最蠢的做法，成本高、日志多、还会触发风控。登录接口的真正价值在"它和别的接口对同一个状态的处理是否一致"。我要看的是：登录成功和失败返回的差异粒度（能不能枚举）、token签发在哪里校验（网关还是应用）、有没有绕过登录直接到达后端的路径、密码重置和登录是否共享同一套凭证校验逻辑。',
'''登录接口我只关心四件事，**爆破排最后**。

**1. 响应差异粒度 → 能枚举账号吗**
- 用户名不存在 vs 密码错误，状态码/提示文案/响应长度/响应时间是否一致
- 只看文案一致不算，要看：失败响应是否也走了完整密码哈希（时间侧信道）
- 手机号/邮箱登录接口，往往能直接确认账号存在

**2. Token 在哪一层签发和校验**
- 登录成功后 token 是 JWT 还是不透明串？JWT 就解 payload 看算法、看是否含 role
- 关键：**token 校验发生在网关还是应用**。如果是网关校验，绕过网关直连后端（内网/IP+Host）往往直接免认证
- 找「未授权也能访问但返回空数据」的接口，说明认证在应用层、且逻辑判断有缺陷

**3. 有没有绕过登录到后端的第二条路**
- 老版本接口 `/api/v1/login`、移动端 `/app/login`、内部 `/inner/login`——多端并存是重灾区
- 密码重置 / 短信验证码接口是否复用同一 credential 校验逻辑（很多站验证码校验在第一步，第二步换手机号就过）

**4. 状态变更类接口**
- 登录后的「绑定手机/邮箱/修改密码」接口，是否校验旧凭证
- 越权：用 A 的 token 改 B 的资料，看后端是否只靠前端传的 userId

爆破只有在**没有验证码、没有锁定、且账号可枚举**时才考虑，且必须限速——打挂业务是事故，不是成果。'''))

S.append((
'你对反序列化有什么理解？',
'反序列化的本质不是"payload构造"，是"数据到对象的映射过程中，谁的字段谁负责"。漏洞出现在映射边界上：序列化器把一个字符串当作类名/方法名/路径去解释，而这个字符串来自外部。所以找反序列化洞，我不找 gadget 链，我先找"解释器"——谁在把外部数据当代码/当标识符用。',
'''反序列化我理解成一句话：**任何"解释器 + 状态 + 反馈 + 持续生成同构单元"的系统，天然存在朊病毒式漏洞。**

不用急着背 gadget 链。先分清三类：

**1. 类型混淆型（原生反序列化）**
Java `readObject`、PHP `unserialize`、Python `pickle`、.NET `BinaryFormatter`、Ruby `Marshal`。特征是：数据里带类型信息，反序列化时按类型自动调用魔术方法。这类洞的入口是「外部数据进了反序列化函数」，gadget 链只是放大器。
关键判断：**能不能控制类名**。能控类名 → 找链；只能控字段 → 找 __wakeup/__destruct 的副作用。

**2. 结构注入型（序列化格式本身被解释）**
Fastjson/Jackson 的 `@type`、YAML 的 `!!python/object`、XML 的 XXE、JSON 里的 `$ref`、模板引擎的表达式。这类不需要"反序列化函数"，只要**解析器把某个字段当元信息解释**。
判断方式：给目标塞一个不存在的类型名，看报错里是否回显「找不到类 xxx」——回显类名就说明它真的在按类型解析。

**3. 语义反序列化型（最容易被忽略）**
把字符串当路径、当 SQL、当命令、当 URL scheme 去「还原成对象」：
- `Path.of(userInput)` → 路径穿越
- `Class.forName(userInput)` → 类加载
- `ExpressionParser.parse(userInput)` → SpEL/OGNL RCE
- `yaml.load`、`eval`、`new Function`、`child_process.exec` 拼串
这类没有 payload 概念，**它就是一句话**。

**我的判断顺序**
1. 先找 sink：哪些地方把「外部可控字符串」交给了解释器
2. 再看能不能到达：入口 → 中间有没有过滤（黑名单几乎必然可绕）
3. 最后才是构造：黑名单绕不过就改格式（JSON→XML→二进制），关键字绕不过就缩小体积、改编码、换语法等价物
4. 有回显就用回显，没回显就找带外（DNS/HTTP/时间），别一上来就写内存马

**补丁绕过思路**：修复通常只堵了一个入口或加了一个黑名单。同一协议下的**另一个参数**（format 切换、编码参数、子类型字段）往往还是通的——这是同一个解析器的另一条边。'''))

S.append((
'你对 OOM（内存马）有什么理解？',
'OOM 我不当"免杀技巧"看，我当"运行时注入"看。要点是：容器已经加载了我的类，而要发现它，必须知道去哪儿找。所以它的对抗本质是观察面 vs 注入面的博弈。注入面：ClassLoader、Filter、Servlet、Listener、Agent、Thread、Timer。观察面：JVM 工具、 Arthas、内存 dump、class 文件扫描。我要做的是选一个"挂上去但不在常规枚举路径上"的位置。',
'''OOM 内存马我按「注入点」分类，而不是按框架分。

**六类注入面**
1. **Filter/Servlet/Listener 链**：往 `ApplicationFilterChain`、`StandardContext` 的集合里加对象。最容易写，也最容易被 `filterChain` 枚举发现
2. **ClassLoader 藏类**：自定义 ClassLoader 加载字节码，类不在 system classloader 的 classpath 上，扫描 `ClassLoader.loadedClasses` 时可能漏
3. **Agent 型**：`Instrumentation.retransformClasses` 改已有类的字节码。没有新增类，常规扫描看不到新类名
4. **线程型**：起一个 Thread 轮询，或者在已有线程池里塞任务。看 `jstack` 能发现，但不看线程就发现不了
5. **Timer/Task 型**：Spring 的 `ScheduledTask`、`TimerTask`
6. **框架扩展点**：Spring 的 `RequestMappingHandlerMapping`（加一个 Controller）、`Interceptor`、`WebMvcConfigurer`

**选择逻辑（关键）**
- 目标有热部署/自动扫描 → 别往子容器里塞，选 Agent 或 ClassLoader
- 目标有 RASP 但不拦反射 → 走反射调用已存在的类
- 目标有完整 jstack/dump 巡检 → 别起线程，藏在已有线程的上下文里
- 关键：**注入的对象最好是"已存在类型的新实例"**，新类名是最明显的特征

**检测对抗的现实**
- `arthas`/`jcmd`/`jmap` 只能看到"有这个东西"，看不到"它是什么时候来的"
- 真正难发现的是**改字节码的 Agent 型**——没有新类、没有新线程、没有新集合元素，只有方法体变了
- 所以：能改字节码就不加 Filter；能复用已有类就不新建类；能不加线程就不加线程

**回显**：容器型用 `ThreadLocal` 拿 request/response；Agent 型可以直接 hook 一个已有的输出方法。没回显就时间盲或文件落地，别假设对方会给你一个 TCP 出口。'''))

S.append((
'代码审计第一步看什么？',
'不看路由，看依赖。依赖的版本决定了"已知洞能不能直接用"；路由决定"能不能到达"。顺序应该是：先定框架和版本（composer.json/pom.xml/package.json），把已知 CVE 列出来，然后带着"我要找哪条链"的目的去看代码，而不是漫无目的地读。',
'''代码审计我的顺序是**先依赖、后入口、再 sink**。

**一、依赖清单 = 攻击面清单**
- PHP：`composer.json`/`composer.lock`
- Java：`pom.xml`/`build.gradle`/`lib/*.jar`（jar 名常带版本）
- Node：`package.json`/`package-lock.json`
- Python：`requirements.txt`/`Pipfile.lock`
拿到版本立刻对照已知 CVE——这一步能省掉 80% 的读码时间。很多项目漏洞不是自己写的，是依赖带进来的。

**二、入口（攻击面）**
- 路由文件：Spring 的 `@RequestMapping`、ThinkPHP 的 `route/`、Express 的 `app.get`
- 过滤器/中间件链：认证在哪一层做、有没有路径白名单（白名单是绕过入口）
- 反序列化入口：`ObjectInputStream`、`unserialize`、`yaml.load`、`readObject`
- 文件操作入口：上传、下载、解压、导出、模板渲染

**三、sink 优先级**
1. 命令执行：`Runtime.exec`、`ProcessBuilder`、`exec`、`system`、`popen`
2. 反序列化
3. SQL 拼接（`${}`、字符串 +）
4. 路径拼接（`File(userInput)`、`Path.resolve`）
5. 表达式解析（SpEL/OGNL/EL/Freemarker）
6. SSRF（`HttpClient` + 用户可控 URL）

**四、判断"能不能到达"**
光有 sink 没用，要能构造一条从入口到 sink 的路径，中间经过的过滤是什么。黑名单几乎必然可绕——**注意黑名单只覆盖了它想到的语法**。
最后：补丁对比。`git log` 里找安全相关的 commit，看它修了什么、修的是不是只有一个入口。修复通常只堵一条路，同一功能的另一个参数往往还通。'''))

S.append((
'GitHub 上看到一个项目，想找它的漏洞，怎么入手？',
'先看它是不是真的在处理不可信输入。很多项目没有攻击面。我的做法：clone 下来先 grep sink，再反查调用链。有 CI/issue 的地方最好——issue 里的 bug 描述就是现成的路径提示。',
'''我的顺序是 **grep sink → 反查调用链 → 查补丁历史**。

**一、先 grep sink（5 分钟出结论）**
```bash
grep -rn "exec\\|system(\\|popen\\|eval(\\|unserialize\\|pickle.loads\\|yaml.load" --include=*.py --include=*.php --include=*.js | head -40
grep -rn "@RequestMapping\\|app.get(\\|router.post(" | head -40
grep -rn "readObject\\|ObjectInputStream\\|Class.forName" | head -20
```
有 sink 才有戏，没有就直接放弃这个项目。

**二、反查：谁能控制到 sink 的参数**
从 sink 往上找调用者，注意：
- 参数是不是来自 `request.getParameter`/`req.body`/`$_GET`
- 中间有没有白名单（有白名单就看白名单是不是可绕）
- 有没有类型转换（int 化就废了）

**三、找"中间层错位"**
- 项目自带 nginx/tomcat 配置吗？配置里的路径规范化规则和代码里的路由匹配规则是否一致
- 有反代/网关的项目，注意 `X-Forwarded-*` 是否被直接信任
- 多模块项目：内部 API 是否假设"只有内网会调"从而不做鉴权

**四、补丁历史是最好的老师**
- `git log --grep="security\\|CVE\\|fix"` 看修过什么
- 看修复 commit 的 diff：它加过滤的位置，往往就是原来漏的位置；**它只修了一个入口的情况极多**
- 版本对比：拿旧版本 tag 和新版本 diff，安全修复会非常显眼

**五、别忽略低代码量模块**
CLI 工具、脚本、示例代码（`examples/`、`scripts/`）常常假设输入可信，是最容易出洞的地方。'''))

S.append((
'一个 Java 站，有 Shiro，怎么快速判断能不能打？',
'不急着上 payload。先确认三件事：密钥是不是默认的、rememberMe 有没有开、返回的 cookie 有没有 Base64 特征。这三件事一个请求就能看出来。',
'''三步定生死，**都不需要构造链**。

**1. 打一个不存在的路径，看 Set-Cookie**
```
GET /nonexistent-abc HTTP/1.1
```
返回 `Set-Cookie: rememberMe=deleteMe` → Shiro 存在且 rememberMe 生效。
注意：有些版本只在登录页返回，先试 `/`、`/login`、`/index`。

**2. 判断密钥**
`rememberMe` 的值是 `AES-CBC(序列化数据, key)` 后 Base64。用默认密钥列表（`kPH+bIxk5D2deZiIxcaaaA==`、`2AvVhdsgUs0FSA3SDFAdag==` 等十几个常见值）离线试解密：
- 解出 `java.io.Serializable` 或 `rO0AB` 特征 → 默认密钥，直接可打
- 解密失败但长度合理 → 自定义密钥，需要爆破（CBC 有 padding oracle 的可能，也可用 AES key 爆破工具跑字典）

**3. 确认 gadget**
- 有 CC 链（CommonsCollections）→ 直接命令执行
- 没 CC 但有 CB1/BeanUtils/Spring 链 → 换链
- 全部不行 → 用 `URLDNS` 做探测（只发 DNS，不执行）

**注意几个实战细节**
- Shiro 1.2.4 之前默认硬编码密钥；1.2.5+ 需要换 key，但很多项目从别处抄了同一个 key（群里流传的 key 大量重复）
- `rememberMe` 长度超过 2000 可能被 WAF 或 Tomcat 的 `maxHttpHeaderSize` 拦，切分成多个请求或直接 POST body
- **打之前先用 URLDNS 确认反序列化真的触发了**，别拿 payload 直接上生产
- 如果 Set-Cookie 不返回但站点表单里有 `_rememberMe` 参数，也可能是 Shiro'''))

S.append((
'遇到 WAF 拦截，怎么继续？',
'WAF 拦的是"它认识的语法"，不是"语义"。所以我先测它的识别粒度：换关键字、缩体积、换格式。这三步基本能定位它拦的是什么层面。如果还不行，我就打它看不见的地方——非 HTTP 端口、内部接口、静态资源路径。',
'''我的四步法，**顺序不能乱**。

**1. 先定性：它拦的是什么**
同一语义，用不同写法发三次：
- 关键字原样：`union select`
- 换成等价语法：`union%0aselect`、`union/**/select`、`UnIoN SeLeCt`
- 换成另一条链：不用 union，用时间盲/布尔盲/报错

三次结果如果「原始被拦、变体通过」→ 关键字黑名单，好绕。
如果三次都拦 → 内容检测或语义分析，要换思路。
如果原始不拦、变体被拦 → 说明它检测的是**异常字符频率**，那就降低熵。

**2. 缩体积**
WAF 的检测有长度上限（常见 8K/16K）。把 payload 拆成多段：分段注入、拼接、二次写入（先写文件再执行）、利用已有函数。
体积小还有一个好处：绕过基于统计模型的检测。

**3. 换格式（最有效）**
- JSON → XML → multipart → 二进制（protobuf/msgpack）
- 参数位置：query → body → header → cookie → path
- 编码：URL → 双重 URL → unicode → base64 → gzip（看服务端解压）
- **原生二进制协议是绕 WAF 的最佳选择**，因为 WAF 通常只解析 HTTP 语义层

**4. 打它看不见的**
- 源站 IP（历史 DNS、证书透明日志、子域名、邮件头）
- 非标准端口/内部服务
- 静态资源路径上的老接口
- CDN 回源时的 Host 混淆（`Host: internal.api`）
- 直接找内网第二个入口：管理后台、运维接口、上传接口

**关键认知**：WAF 绕不过，往往是**攻击链设计错了**，不是 payload 不够花。换一条不需要过 WAF 的链，比硬刚 payload 省时间。'''))

S.append((
'中间件版本没更新，但漏洞补丁打了，这种怎么搞？',
'补丁绕过是0day产地，思路是看补丁改了什么、它在哪一层加校验。绝大多数补丁是"在入口加黑名单"，而不是"修根因"。所以只要找到同一功能的另一条入口，或者同一入口的另一种语法，就通了。',
'''补丁绕过我的方法论：**定位修复点 → 找同类入口 → 找等价语法**。

**1. 拿到补丁 diff**
- 开源项目：`git log` 找安全 commit，看改哪个文件哪一行
- 闭源产品：版本对比（jar 反编译对比、dll 对比），或者看 changelog 里的措辞
- 重点看：新加的 `if`/`filter`/`replace` 检查的是什么字符/什么条件

**2. 判断补丁层次（决定能不能绕）**
- **入口黑名单**（最常见）：只拦了它想到的字符 → 换等价语法/换编码/换参数位
- **参数级校验**：只校验了某个参数 → 同一功能的其他参数、其他接口没校验
- **根因修复**：真的把危险操作去掉了（如换了解析器）→ 换攻击面，别硬刚

**3. 找同类入口（成功率最高）**
补丁往往只修了**被报告的那一条路径**：
- Web 修了，CLI/API/定时任务/导入功能没修
- 主版本修了，老版本接口（`/api/v1`）没修
- 前端参数校验加了，后端接口直接调没加
- 多租户/多端（小程序、APP、第三方对接）往往共用代码但入口不同

**4. 找等价语法（同一解析器另一条边）**
- 反序列化：JSON 的 `@type` 被拦 → 试 XML 的 `type` 属性、试 YAML、试 `format` 参数、试 `Content-Type`
- 模板注入：`${}` 被拦 → 试 `#{}`、`*{}`、`@{}`、`%{}`
- 命令执行：`|` 被拦 → 试 `$()`、反引号、`%0a`、参数注入（`--exec`）
- 路径穿越：`../` 被拦 → 试 `..%2f`、`....//`、`..\\`、绝对路径、软链接

**5. 验证要看响应字节**
不要看 HTTP 状态码。要看：
- 报错信息里有没有明文代码/类名（有 → 说明进入了危险分支）
- 响应长度/时间是否有微小差异（说明部分执行了）
- 带外有没有回连（DNS/HTTP）

如果理论说"不触发"但实测有差异，**以实测为准**。'''))

S.append((
'怎么从 JS 里挖 API 和密钥？',
'前端 JS 是后端的镜子。它暴露的不是代码，是"后端有哪些接口、参数叫什么、鉴权怎么做"。所以我不做代码审计，我做接口清单重建。',
'''JS 挖 API 的核心是**重建接口清单 + 找鉴权模型**。

**一、拿全部 JS**
- 从首页 HTML 拿入口 bundle，再从 bundle 里的 import 链递归下载
- 用 `main.js` 里的 chunk map（webpack 的 `webpackChunk` 或 `__vite__mapDeps`）枚举所有懒加载文件
- sourcemap 没关的话（`.js.map`）直接拿原始源码

**二、提取路径（三个层次）**
1. **字面量**：`"/api/user/info"`、`"/v1/order/detail"`
2. **模板拼接**：`` `/api/${version}/user` `` → 找 version 的来源
3. **动态构造**：拼 baseURL 的地方，通常是拦截器/请求封装，这里能看到：
   - 统一鉴权头（`Authorization`、`X-Token`、自定义签名）
   - 签名算法（`md5(params + salt)`——salt 就在 JS 里）
   - 白名单接口（不需要 token 的那些）

**三、找密钥与配置**
- `appKey`、`secret`、`accessKey`、`aesKey`、`iv`、`salt`、`signKey`
- 云服务：OSS bucket、`sts`、地图 key、推送 key
- 测试/内网地址：`http://10.`、`http://192.168.`、`-test.`、`-dev.` 域名

**四、找"后端有但前端没调用"的接口**
这是最有价值的：
- 老版本接口残留（`/api/v1/`、`/api/old/`、`/api/test/`）
- 内部接口（`/inner/`、`/internal/`、`/admin/`、`/debug/`）
- 前端注释掉的调用

**五、对比鉴权**
JS 里白名单接口（不传 token 的）→ 直接无认证访问，看返回什么。
如果返回数据结构完整但内容为空 → 说明认证在业务层，可能参数越权。'''))

# 追加：方法类
S.append((
'怎么快速判断一个站有没有 SSRF 的可利用面？',
'先找"服务端会替我去访问 URL"的功能：图片抓取、webhook、导入、PDF/截图生成、代理、文件下载。找到后就判断三件事：回显、出网、能不能打内网。',
'''SSRF 我不从 payload 出发，从**功能**出发。

**一、找"服务端代理请求"的功能（这些才是 SSRF 的载体）**
- 图片/头像 URL 上传、文章导入、富文本远程图片
- webhook / 回调 / 通知地址配置
- PDF 生成、网页截图、快照、Markdown 渲染
- 文件下载（`?url=`）、代理、翻译、订阅（RSS）
- 云存储：OSS 的 `x-oss-process`、图床

**二、判断三件事**
1. **回显**：能不能看到响应体？只看得到"成功/失败"就是盲的
2. **出网**：目标能不能访问外网？用 DNS（`xxx.dnslog.cn`）测，不依赖 HTTP
3. **限制**：黑名单（`127.0.0.1`/`localhost`/`169.254`）还是白名单（只允许自家域名）

**三、绕过限制的常见路子**
- 十进制/八进制 IP：`2130706433`、`0177.0.0.1`
- IPv6：`[::1]`、`[::ffff:127.0.0.1]`
- DNS 重绑定（自己控制的域名，第一次解析到公网，第二次到内网）
- 302 跳转（第一次给公网地址，服务端跟随到内网）
- `@` 混淆：`http://allowed.com@127.0.0.1`
- 白名单绕过：`http://allowed.com.evil.com`、`http://evil.com/?allowed.com`

**四、能打到哪里**
- 云元数据：`169.254.169.254`（AWS/阿里云/腾讯云路径不同）、`metadata.google.internal`
- 内网端口探测：根据响应时间/长度差异判断端口开放
- Redis（`gopher://` 写 cron/写 webshell）、MySQL（`gopher://` 未授权）、FastCGI
- K8s：`kubernetes.default.svc`、`/var/run/secrets/...`
- Docker API：`2375`

**关键**：SSRF 的价值不在"能访问内网"，在"能控制协议"。`gopher://` 能打出 RCE，HTTP 只能探测。'''))

S.append((
'你觉得什么样的漏洞最值钱？',
'不是最难打的，是"能直接拿到数据或执行权限、且不需要用户交互"的。所以我优先找：认证绕过、反序列化 RCE、越权读数据。这三个类别的共同点是——打到就是终点，不需要二次利用。',
'''值钱 = **不需要交互 + 直接到终点 + 影响面大**。

**第一档：直接 RCE / 直接读全量数据**
- 反序列化链（不需要用户点链接）
- 认证绕过（直接进后台，不需要密码）
- SQL 注入（能读全库）
- 模板注入 / 表达式注入
- 越权（改一个 id 就能读别人的数据）

**第二档：需要一步辅助**
- SSRF（能打内网但要找可用服务）
- 文件上传（要找到可执行路径）
- 路径穿越（能读文件但要找敏感文件）

**第三档：需要用户交互或条件苛刻**
- XSS（要钓鱼）
- CSRF（要诱导点击）
- 信息泄露（要看泄露的是什么）

**我的取舍**：宁可花 3 天找一个认证绕过，也不花 3 天找一个花式 XSS。因为认证绕过是"能力"，XSS 是"场景"。

**但真正的判断标准是业务**：
- 金融/医疗：读数据比 RCE 更值钱（一次导出全量用户）
- 基础设施/云平台：RCE 或拿集群权限
- SaaS 多租户：跨租户越权 = 灾难

同一个洞，在不同业务里价值差 10 倍。'''))

out = []
for u, th, a in S:
    out.append({'messages': [
        {'role': 'user', 'content': u},
        {'role': 'assistant', 'content': a, 'reasoning_content': th},
    ]})

with open('/tmp/seed.jsonl', 'w', encoding='utf-8') as f:
    for r in out:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')

print('SEED', len(out))
print('chars', sum(len(u) + len(th) + len(a) for u, th, a in S))
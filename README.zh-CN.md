[English](README.md) · **简体中文** · [Français](README.fr.md) · [العربية](README.ar.md)

# formist-cli

**`fm` 是 [Formist](https://formist.ai) 和 [Evane](https://evane.ai) 部署的终端客户端,
走的是和网页版完全相同的 API 和登录方式。**

![终端里运行 fm health、fm spaces 和 fm kb search,连接一个 Formist 部署](docs/screenshot.png)

<sub>示例数据,用真实的 `fm` 代码通过 `python3 scripts/render_screenshot.py` 渲染而成。</sub>

---

## 问题

维护一个 Formist 或 Evane 部署,每天都要问它一些琐碎但紧急的问题:这个区域健康吗,
某个连接器是不是卡住了,昨天的迁移是不是所有地方都跑完了,这么多空间里哪个是这个
客户的。通常每一个问题都要打开网页版,点进管理页面找答案,或者写一个一次性脚本直
接调 API,还得自己处理登录。两种方式都不能写成脚本复用,偏偏越是要快的时候越慢。

## 思路

`fm` 用和网页版一样的方式登录,所以它能做的事,正好就是你这个账号在网页版能做的事,
不多也不少。每条命令执行前都会先打印它解析出来的部署、空间和凭证,所以屏幕上看到
的就是完整情况,不用猜。会改数据的命令,永远先把要做的事打印出来,提交是单独的、
明确的一步。

- **一个客户端,覆盖所有部署。** `--host` 在 Formist、Evane、阿联酋 Evane、中国区
  Formist,或任意自定义地址(比如本地开发服务器)之间切换,命令和用法完全一样。
- **先说清楚来源,再动手。** 每条命令都会报告它解析出的部署和空间,以及这些值是
  从哪里来的:一个参数、一个记住的默认值,还是什么都没有。目标选错了,看屏幕就能
  发现,不用等出了事才知道。
- **默认先试运行。** 会改数据的命令,先打印它会做什么,等着你传 `--apply`。合并
  重复行的 `fm participants dedupe` 更进一步:提交前会重新核对这份计划是否还和刚
  才的试运行一致,防止一次过时的读取变成一次错误的写入。
- **凭证按部署分开存。** `fm login` 把 OAuth 凭证存成 `0600` 权限的文件,每个部署
  一个文件,登录测试环境绝不会动到生产环境。CI 场景用 `FORMIST_TOKEN` 提供凭证:
  读到就用,不落盘,不自动刷新。
- **没有意外的依赖。** 只用标准库,Python 3.10 以上,一秒装完。

## 安装

```sh
pipx install formist-cli
```

需要 Python 3.10 或更高版本。没有第三方依赖。

## 用法

### 登录

```sh
fm login            # opens a browser; the credential is stored at 0600
fm whoami
fm logout
```

没有浏览器的机器上,用 `fm login --no-browser`,把页面显示的码粘贴进去。CI 环境里,
把 `fm login` 写出的文件里 `token` 字段的值设成环境变量 `FORMIST_TOKEN` 即可;这个
值只会被读取使用,不会写入磁盘。

凭证存在 `~/.config/formist/`(可以用 `FORMIST_CONFIG_DIR` 改路径),每个部署一个
文件,所以登录一个部署不会替换掉另一个的凭证。

平台管理操作需要额外的授权:`fm login --admin` 会申请这个授权,但只有账号本身已经
拥有该权限,服务器才会批准。不加这个参数的话,哪怕本来是平台管理员,这个凭证也只
能当普通用户用。

### 选择部署

`fm` 默认访问 `https://api.formist.ai`。用 `--host` 可以切到别的部署:

| `--host`     | API                                  |
|--------------|---------------------------------------|
| `formist`    | `https://api.formist.ai`(默认)      |
| `evane`      | `https://api.evane.ai`               |
| `evane-ae`   | `https://ae.api.evane.ai`            |
| `formist-cn` | `https://formist-api.we-carbon.com`  |

其他任何值都会被当成一个 API 地址,比如连本地服务器用
`fm --host 127.0.0.1:8160 spaces`。`--host` 放在命令前后都可以:
`fm --host evane spaces` 和 `fm spaces --host evane` 是一回事。

### 空间

空间永远用它的 id 来指定,不能用名字,因为名字不保证唯一:

```sh
fm spaces                         # your own spaces
fm use --space <space-id>         # remember a default space
fm kb search "venue contract" --space <space-id>
```

`fm use` 也可以记住一个 host,但只在读操作里生效。写操作永远不会继承记住的 host:
要么用 `--host` 显式指定,要么就用默认值 `formist`。

### 写操作

大多数会改数据的命令都会先试运行,打印出它会做什么;加上 `--apply` 才会真正提交。
`fm participants dedupe` 稍微不一样,它会合并一组重复记录:试运行会打印一个计划
令牌(plan token),`--apply <令牌>` 会重新跑一次试运行,确认这份计划还和当初一
致,然后才提交。少数不可撤销的操作,比如 `fm spaces --admin delete`,根本没有试
运行这一步,各自的 `--help` 里会写清楚。

### 命令

完整命令列表看 `fm --help`,每条命令的细节看 `fm <command> --help`。每条命令都
只用到你这个账号本身的权限,和网页版一样,服务器每次调用都会重新核实权限:
`fm spaces` 列出你所在的空间,`fm health` 不需要登录就能用,`fm doctor` 把健康
检查汇总到所有部署;剩下的 `chats`、`chat`、`kb`、`get`、`export`、`import`、
`diff`、`upload`、`skill-attach`、`entity-fields`、`skill-library-install`、
`meeting`、`project`、`connector`、`skills-add`、`skills-update`、`proposals`、
`audit-log`、`connectors`、`files`,也都是以同样的方式读写你自己的空间。

### 任务(`fm task`)

`fm task` 内置了 [`nod`](https://github.com/wecarbon/noddit) 那套任务与决策跟踪器:一个节点就是
一件事,AI 能直接做的是任务,需要负责人先拍板的是决策。它有两种模式:

- **未登录**,在任意项目里:`fm task init` 会建一个 `decisions/` 文件夹,下面每条命令都从当前
  目录往上找它,和 `git` 找 `.git` 的方式一样。什么都不会发出去。
- **已登录**,配合 `fm login`:`fm task --host <部署>`(或者 `fm use` 记住的、带凭证的 host)会
  改成调用那个空间的节点接口,这样一个团队就能通过 Formist/Evane 共用一套跟踪器,而不是各自
  一份本地文件夹。不是每个子命令在云端都已经接通,没接通的会直接说明,不会瞎猜。

```sh
fm task init                                  # creates decisions/ here
fm task add "Write the onboarding doc" --task # a new node
fm task start X1                              # to do -> in progress
fm task note X1 "found last year's draft"     # progress, without closing it
fm task show active                           # every open node (bare `show` lists only ones needing a decision)
fm task done X1 --note "doc is up"            # -> done
```

决策用的是同一套命令,只是把 `--task` 换成几个选项:

```sh
fm task add "Which domain for staging" -o 1="api-staging.example.com" -o 2="staging-api.example.com"
fm task push X2 1 --note "matches the cert"   # the owner's answer, recorded on the node
```

每次写操作都要有作者:传 `--author "<你> · 在做什么"`,或者整个会话提前导出一次
`NODDIT_AUTHOR`。`fm task whoami` 会报出一次写操作会签成谁:云端模式下是登录账号和空间,
本地模式下就是写进每条笔记里的那个身份。`fm task briefs` 列出负责人派过、但还没人接手的活;
`fm task claim <节点>` 接下一个。完整子命令列表看 `fm task --help`,每个子命令的参数看
`fm task <子命令> --help`。

#### Claude Code 插件

这个 git 仓库本身(不是 `pipx` 装的那个包)同时也是一个 Claude Code 插件:`hooks/hooks.json`
会在每个会话开始时注入 `fm task` 的用法规则和项目里还没完成的节点,`monitors/monitors.json`
会起 `fm task monitor --here`,让负责人的答复能传到 AI 那里,不用它自己去轮询。这个插件还没
发布到插件市场,所以要从仓库的副本里加载,而不是已安装的包:

```sh
git clone https://github.com/WeCarbon/formist-cli
claude --plugin-dir formist-cli
```

## 常见问题

**用这个工具需要平台管理员权限吗?**
不需要。默认情况下每条命令都只用到你这个账号本身的权限,和网页版给这个账号的权限
完全一致,比如 `fm spaces` 列出的就是你所在的空间。少数命令还能加 `--admin`,
专门给平台管理员账号用;不加这个参数,或者账号没有对应的授权,它们会在本地就被拒
绝、请求都不会发出去,最终还是由服务器说了算。

**能不能指向本地开发服务器?**
可以。`--host` 接受任意 API 地址,不只是那四个预设部署,比如
`fm --host 127.0.0.1:8160 health`。

**能不能在 CI 里用,不走浏览器登录?**
可以。把 `fm login` 写出的文件里 `token` 字段的值设成环境变量 `FORMIST_TOKEN`。
每次调用都会重新读取这个值,读到就用,`fm` 既不会把它写入磁盘,也不会自动刷新。

**什么机制能防止写操作发到错的部署上?**
`fm use` 记住的 host 只在读操作里生效;写操作永远从命令行解析 `--host`,不传就
用默认值 `formist`。写操作没有"当前环境"这种东西可以让它悄悄继承。

**`fm` 会不会做出这个账号在网页版做不到的事?**
不会。它调用的是同一个 API,用的是同一个凭证,每次调用服务器都会重新核实权限;
`fm` 只是换了一个入口,权限范围没有任何变化。

## 开发

```sh
uv sync
uv run pytest
```

`docs/screenshot.png` 是从真实的 `fm` 输出渲染出来的,连接一个本地模拟服务器,不
是手画的:

```sh
python3 scripts/render_screenshot.py
```

## 许可证

[MIT](LICENSE) © 2026 WeCarbon

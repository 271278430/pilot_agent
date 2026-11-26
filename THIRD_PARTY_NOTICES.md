# 第三方来源与许可证

本项目使用官方 DeepSeek Harness 作为基础框架，未修改其核心执行循环。

- GitHub：https://github.com/deepseek-ai/deepseek-harness
- 源码参考 commit：`fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`
- 官方 Python SDK：`deepseek-harness-sdk==0.2.1rc2`
- 官方 npm runtime：`@deepseek-ai/dsh@0.2.2-rc.1`
- SDK wheel 配套 binary 在 Windows 持久化时遇到原生模块加载问题；项目使用官方 npm CLI 作为 SDK 的 `dsh_bin`。
- mini-SWE-agent 2.4.6 是可选外部对照基线，来源 https://github.com/SWE-agent/mini-swe-agent，MIT。

依赖的精确版本见 `requirements.lock` 和 `harness/runtime/package-lock.json`；各依赖的许可证随安装包或上游仓库提供。

InfraPilot 使用 [PyTorch](https://github.com/pytorch/pytorch)、[Transformers](https://github.com/huggingface/transformers)、[PEFT](https://github.com/huggingface/peft)、[FastAPI](https://github.com/fastapi/fastapi) 和 [Paramiko](https://github.com/paramiko/paramiko)。

模型为 [HuggingFaceTB/SmolLM2-135M-Instruct](https://huggingface.co/HuggingFaceTB/SmolLM2-135M-Instruct)，固定 revision 见源码配方，模型元数据声明 Apache-2.0；权重不放入 Git。`evals/external/boltons-source.tar.gz` 包含用于评测的公开 boltons 源码，其许可证位于归档内。

DeepSeek Harness 的 MIT 许可如下：

```text
MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

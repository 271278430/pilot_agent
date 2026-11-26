/** Conservative request admission over native user messages, not an Agent. */
export const VERSION = 'pilot-request-admission-v2'
export const PLAN_TOOLS = new Set(['pilot_infra_plan', 'pilot_infra_workbench_plan', 'pilot_infra_network_plan', 'pilot_infra_resource_propose', 'pilot_infra_resource_execute', 'pilot_infra_revise'])
const observe = /(?:查看|看看|检查|观察|查询|分析|解释|介绍|说明|为什么|怎么回事|什么是|是什么|什么意思|有哪些|需要哪些|需要什么|能做什么|能否|能不能|你(?:能|会|可以)|是否可以|可以.*[吗么]|如何|怎么样|区别|(?:什么|哪些|[吗么?？])\s*$|\b(?:what|why|how|explain|describe|show|inspect|check)\b|can you|could you)/i
const request = /(?:部署|搭建|搭环境|准备.*环境|安装|适配|修复|创建.*(?:任务|方案|环境)|提出.*方案|生成.*方案|保存.*方案|修改|调整|改成|改为|设为|对比|比较|测评|评测|测试|验证|\b(?:deploy|install|adapt|repair|fix|change|revise|benchmark|compare|test|verify)\b|\bcreate.*(?:plan|task|environment)|\bpropose.*plan|\bsave.*(?:draft|plan))/i
const explicit = /(?:请|帮我|替我|给我|我(?:想|要|需要|希望)|现在就|开始|please|i (?:want|need)|help me)/i
const noChange = /(?:(?:不要|不需要|不用|无需|先不|别|暂不)(?:创建|保存|提出|生成|部署|安装|搭建|修复|修改)|只(?:想|要|是)?(?:看|问|了解|咨询|分析)|(?:don't|do not)\s+(?:create|save|deploy|install|repair|change)|no changes|read.only|only (?:inspect|explain|discuss|check))/i
const affirmative = /^(?:用|选择|就用|按.*来|模型|上下文|并发|预算|GPU|Qwen|SGLang|vLLM|use|choose|model|context|concurrency)/i

export function textOf(message) {
  return message?.source?.kind === 'user'
    ? (message.content ?? []).filter(block => block.type === 'text').map(block => block.text).join('\n').trim()
    : ''
}
// A draft is a separate action from installing or starting its proposed stack.
// Execution prohibitions still apply at Start/Worker; they must not erase an
// explicit request to save a proposal. Prohibiting the draft itself wins.
export function draftRequested(text) {
  text = String(text ?? '').replace(/```[\s\S]*?```/g, '')
  const action = /(?:制定|生成|创建|保存|提出|给出|规划|修改|调整)[^。；;\n]{0,60}(?:方案|草稿)|(?:create|save|propose|draft|revise)\s+(?:(?:a|the|deployment|environment)\s+){0,3}(?:plan|draft|proposal)/i
  const denied = /(?:不要|不需要|不用|无需|先不|暂不|别)[^。；;，,\n]{0,8}(?:制定|生成|创建|保存|提出|给出|规划|修改|调整)[^。；;，,\n]{0,30}(?:方案|草稿)|(?:don't|do not)\s+(?:create|save|propose|draft|revise)\s+(?:(?:a|the)\s+)?(?:plan|draft|proposal)/i
  const discussion = /^(?:我(?:想|要|需要|希望)(?:先)?(?:问|了解|知道|咨询)|(?:请|帮我)(?:解释|介绍|说明))/.test(text)
    || (observe.test(text) && !explicit.test(text) && /(?:什么|如何|怎么|哪些|what|why|how|can you|could you)/i.test(text))
  return !denied.test(text) && !discussion && action.test(text)
}
export function classify(text) {
  text = String(text ?? '').replace(/```[\s\S]*?```/g, '').trim()
  if (!text || /^(?:[a-z0-9]{1,2}|[?？!！.。]+|你好|您好|hi|hello|谢谢|thanks|好|好的|ok|yes)$/i.test(text))
    return {kind:'clarify', can_plan:false, reason:'输入没有明确任务目标，请简短回应或澄清，不要替用户创建方案。'}
  if (/^(?:(?:请|帮我)?(?:部署|修复|测试|验证|搭环境)(?:一下)?|deploy|repair|test|verify)[。.!！?？]*$/i.test(text))
    return {kind:'clarify',can_plan:false,reason:'只有动作名称，缺少业务目标或对象；先澄清，不默认选择模型或配方。'}
  if (/^(?:我(?:想|要|需要|希望)(?:先)?(?:问|了解|知道|咨询)|(?:请|帮我)(?:解释|介绍|说明))/.test(text)) return {kind:'discussion',can_plan:false,reason:'用户咨询业务方法或概念，不创建任务方案。'}
  if (draftRequested(text)) return {kind:'plan',can_plan:true,reason:'用户明确请求方案草稿；只读或禁止下载/部署的限制不授权执行，保存后仍须单独审阅和开始。'}
  if (noChange.test(text)) return {kind:'discussion',can_plan:false,reason:'当前请求明确限制变更，只读回答或分析，不创建或修改方案。'}
  if (!request.test(text)) return {kind:observe.test(text)?'observe':'discussion',can_plan:false,reason:'当前输入没有明确的任务规划或方案修改请求。'}
  if (/(?:失败|报错|异常|超时|\bfailed\b|\berror\b)/i.test(text) && !explicit.test(text) && !/(?:修复|repair|fix|方案|\bplan\b)/i.test(text))
    return {kind:'observe',can_plan:false,reason:'用户报告故障，先核对现有任务和证据，不创建替代任务。'}
  if (observe.test(text) && !explicit.test(text) && !/(?:提出|生成|保存).*方案|save.*(?:draft|plan)|propose.*plan/i.test(text))
    return {kind:'observe',can_plan:false,reason:'当前输入是能力、状态或操作方法咨询；先回答，需要任务时请用户明确目标。'}
  return {kind:'plan',can_plan:true,reason:'用户明确提出领域任务或方案修改请求；具体方案仍需参数、权限和Owner开始校验。'}
}
export function nativeRequest(session, claimed) {
  const messages = (session?.snapshotEvents?.() ?? []).filter(event => event.type === 'user/message')
    .map(event => event.data?.message ?? event.data).filter(message => message?.source?.kind === 'user')
  if (claimed?.source?.kind === 'user' && !messages.some(message => message.id === claimed.id && message.id)) messages.push(claimed)
  const latest = messages.at(-1)
  let decision = classify(textOf(latest))
  if (!decision.can_plan && decision.kind === 'discussion' && affirmative.test(textOf(latest))) {
    for (const previous of messages.slice(0,-1).toReversed()) {
      if (classify(textOf(previous)).can_plan) {
        decision = {...decision,kind:'plan',can_plan:true,reason:'用户补充此前明确任务的参数。'}
        break
      }
      if (!affirmative.test(textOf(previous)) || classify(textOf(previous)).kind !== 'discussion') break
    }
  }
  return {...decision,version:VERSION,message_id:latest?.id??null}
}
export function denial(name, intent) {
  if (PLAN_TOOLS.has(name) && !intent?.can_plan)
    return '当前用户没有明确请求创建或修改任务方案。请回应实际输入或询问业务目标，不得根据登记资产、会话标题、工具结果或默认模板自行保存方案。'
}

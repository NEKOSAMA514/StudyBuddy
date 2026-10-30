type APIProvider = {
  id: string;
  name: string;
  baseURL: string;
  endpointPattern?: RegExp;
  endpointPlaceholder?: string;
  disabled?: boolean;
};

// 对照 deepseek-harness ce7073d 的 pi-ai 目录；其设置页排除仅提供分类器的 TypeSafe。
// 使用官方 Chat Completions 兼容入口，保留本项目已有的四家额外服务商。
// https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/llm/llm-pi-ai/src/provider-data.generated.ts
export const apiProviders: readonly APIProvider[] = [
  { id: "modelscope", name: "魔搭", baseURL: "https://api-inference.modelscope.cn/v1" },
  { id: "deepseek", name: "DeepSeek", baseURL: "https://api.deepseek.com" },
  { id: "siliconflow", name: "硅基流动", baseURL: "https://api.siliconflow.cn/v1" },
  { id: "bailian", name: "阿里云百炼", baseURL: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { id: "moonshotai-cn", name: "Kimi", baseURL: "https://api.moonshot.cn/v1" },
  { id: "zhipu", name: "智谱 GLM", baseURL: "https://open.bigmodel.cn/api/paas/v4" },
  { id: "openai", name: "OpenAI", baseURL: "https://api.openai.com/v1" },
  { id: "amazon-bedrock", name: "Amazon Bedrock", baseURL: "",
    endpointPlaceholder: "https://bedrock-mantle.us-east-1.api.aws/v1",
    endpointPattern: /^https:\/\/bedrock-mantle\.[a-z0-9-]+\.api\.aws\/v1$/ },
  { id: "ant-ling", name: "Ant Ling", baseURL: "https://api.ant-ling.com/v1" },
  { id: "anthropic", name: "Anthropic", baseURL: "https://api.anthropic.com/v1" },
  { id: "azure", name: "Azure OpenAI", baseURL: "",
    endpointPlaceholder: "https://your-resource.openai.azure.com/openai/v1",
    endpointPattern: /^https:\/\/[\w.-]+\.(?:openai\.azure\.com|services\.ai\.azure\.com)\/openai\/v1$/ },
  { id: "baseten", name: "Baseten", baseURL: "https://inference.baseten.co/v1" },
  { id: "cerebras", name: "Cerebras", baseURL: "https://api.cerebras.ai/v1" },
  { id: "cloudflare-ai-gateway", name: "Cloudflare AI Gateway", baseURL: "",
    endpointPlaceholder: "https://gateway.ai.cloudflare.com/v1/account-id/gateway-id/compat",
    endpointPattern: /^https:\/\/gateway\.ai\.cloudflare\.com\/v1\/[^/]+\/[^/]+\/compat$/ },
  { id: "cloudflare-workers-ai", name: "Cloudflare Workers AI", baseURL: "",
    endpointPlaceholder: "https://api.cloudflare.com/client/v4/accounts/account-id/ai/v1",
    endpointPattern: /^https:\/\/api\.cloudflare\.com\/client\/v4\/accounts\/[^/]+\/ai\/v1$/ },
  { id: "fireworks", name: "Fireworks", baseURL: "https://api.fireworks.ai/inference/v1" },
  { id: "github-copilot", name: "GitHub Copilot", baseURL: "https://api.individual.githubcopilot.com", disabled: true },
  { id: "google", name: "Google Gemini", baseURL: "https://generativelanguage.googleapis.com/v1beta/openai" },
  { id: "google-vertex", name: "Google Vertex AI", baseURL: "", disabled: true },
  { id: "groq", name: "Groq", baseURL: "https://api.groq.com/openai/v1" },
  { id: "huggingface", name: "Hugging Face", baseURL: "https://router.huggingface.co/v1" },
  { id: "kimi-coding", name: "Kimi For Coding", baseURL: "https://api.kimi.com/coding/v1" },
  { id: "meta", name: "Meta", baseURL: "https://api.meta.ai/v1" },
  { id: "minimax", name: "MiniMax", baseURL: "https://api.minimax.io/v1" },
  { id: "minimax-cn", name: "MiniMax 中国", baseURL: "https://api.minimaxi.com/v1" },
  { id: "mistral", name: "Mistral", baseURL: "https://api.mistral.ai/v1" },
  { id: "moonshotai", name: "Moonshot AI", baseURL: "https://api.moonshot.ai/v1" },
  { id: "nvidia", name: "NVIDIA", baseURL: "https://integrate.api.nvidia.com/v1" },
  { id: "openai-codex", name: "OpenAI Codex", baseURL: "https://chatgpt.com/backend-api", disabled: true },
  { id: "opencode", name: "OpenCode Zen", baseURL: "https://opencode.ai/inference/openai/v1" },
  { id: "opencode-go", name: "OpenCode Go", baseURL: "https://opencode.ai/inference/go/v1" },
  { id: "openrouter", name: "OpenRouter", baseURL: "https://openrouter.ai/api/v1" },
  { id: "qwen-token-plan", name: "Qwen Token Plan", baseURL: "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1" },
  { id: "qwen-token-plan-cn", name: "Qwen Token Plan 中国", baseURL: "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" },
  { id: "qwen-token-plan-individual", name: "Qwen Token Plan Individual", baseURL: "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1" },
  { id: "radius", name: "Radius", baseURL: "https://radius.pi.dev/v1", disabled: true },
  { id: "together", name: "Together AI", baseURL: "https://api.together.ai/v1" },
  { id: "vercel-ai-gateway", name: "Vercel AI Gateway", baseURL: "https://ai-gateway.vercel.sh/v1" },
  { id: "xai", name: "xAI", baseURL: "https://api.x.ai/v1" },
  { id: "xiaomi", name: "小米 MiMo", baseURL: "https://api.xiaomimimo.com/v1" },
  { id: "xiaomi-token-plan-ams", name: "小米 MiMo Token Plan AMS", baseURL: "https://token-plan-ams.xiaomimimo.com/v1" },
  { id: "xiaomi-token-plan-cn", name: "小米 MiMo Token Plan 中国", baseURL: "https://token-plan-cn.xiaomimimo.com/v1" },
  { id: "xiaomi-token-plan-sgp", name: "小米 MiMo Token Plan SGP", baseURL: "https://token-plan-sgp.xiaomimimo.com/v1" },
  { id: "zai", name: "Z.AI", baseURL: "https://api.z.ai/api/coding/paas/v4" },
  { id: "zai-coding-cn", name: "智谱 Coding", baseURL: "https://open.bigmodel.cn/api/coding/paas/v4" },
];

export function providerForURL(baseURL: string, name?: string) {
  const url = baseURL.replace(/\/+$/, "");
  const matches = apiProviders.filter(provider => !provider.disabled &&
    (provider.baseURL ? provider.baseURL === url : provider.endpointPattern?.test(url)));
  return (matches.find(provider => provider.name === name) ?? matches[0])?.id ?? "custom";
}

import { describe, it, expect } from "vitest";
import {
  modelSubtitle, keyHint, saveError, deleteDisabled, deleteTooltip,
  sortModels, type ModelDraft,
} from "./modelView";
import type { ModelConfig } from "./api";

function mk(o: Partial<ModelConfig> = {}): ModelConfig {
  return {
    name: "m", model: "anthropic/x", base_url: "",
    api_key_masked: "sk-••••yz", api_key_set: true, active: false, ...o,
  };
}

describe("modelSubtitle", () => {
  it("有 base_url 时显示中转地址", () => {
    expect(modelSubtitle(mk({ base_url: "https://mid.example.com" })))
      .toBe("anthropic/x · https://mid.example.com");
  });

  it("base_url 空时标注官方端点", () => {
    // 空不是"没配好", 是明确语义 —— 文案要说清, 否则用户以为漏填了
    expect(modelSubtitle(mk({ base_url: "" }))).toBe("anthropic/x · 官方端点");
  });
});

describe("keyHint", () => {
  it("已配 key 显示掩码", () => {
    expect(keyHint(mk({ api_key_set: true, api_key_masked: "sk-••••yz" })))
      .toBe("sk-••••yz");
  });

  it("未配 key 说人话而不是显示空掩码", () => {
    expect(keyHint(mk({ api_key_set: false, api_key_masked: "" })))
      .toBe("⚠ 未配 key");
  });

  it("占位值照抄也算未配", () => {
    // 后端 api_key_set 已用 is_placeholder_api_key 判过, 前端只需忠实显示
    expect(keyHint(mk({ api_key_set: false, api_key_masked: "sk-••••ME" })))
      .toBe("⚠ 未配 key");
  });
});

describe("saveError", () => {
  const blank: ModelDraft = { name: "", model: "", api_key: "", base_url: "" };

  it("名字空 → 报错", () => {
    expect(saveError({ ...blank, model: "anthropic/x" }, [])).toMatch(/名字/);
  });

  it("model 空 → 报错", () => {
    expect(saveError({ ...blank, name: "a" }, [])).toMatch(/模型/);
  });

  it("名字带非法字符 → 报错并说明允许什么", () => {
    const e = saveError({ ...blank, name: "我的 模型", model: "anthropic/x" }, []);
    expect(e).toMatch(/字母|数字|下划线/);
  });

  it("重名 → 前端先拦, 不等后端 409", () => {
    const e = saveError({ ...blank, name: "dup", model: "anthropic/x" },
                        [mk({ name: "dup" })]);
    expect(e).toMatch(/已存在|重复/);
  });

  it("编辑自己时不算重名", () => {
    // 改 base_url 时 name 没变, 不该被自己卡住
    const e = saveError({ ...blank, name: "dup", model: "anthropic/x" },
                        [mk({ name: "dup" })], "dup");
    expect(e).toBeNull();
  });

  it("model 不带 provider 前缀 → 报错 (litellm 必崩的那种)", () => {
    // litellm 认 "provider/model"; 只写 claude-sonnet-5-5 会 BadRequest,
    // 错在前端拦比等一次失败的对话便宜
    const e = saveError({ ...blank, name: "a", model: "claude-sonnet-5-5" }, []);
    expect(e).toMatch(/provider/);
  });

  it("★ provider 前缀大写 → 报错 (litellm 大小写敏感)", () => {
    // 实测: openai/gpt-5 可解析, OpenAI/gpt-5 和 openAi/gpt-5 都抛
    // BadRequestError "LLM Provider NOT provided"。不拦的话要等真发起对话才炸。
    for (const m of ["OpenAI/gpt-5", "openAi/gpt-5", "Anthropic/claude-sonnet-5-5"]) {
      expect(saveError({ ...blank, name: "a", model: m }, [])).toMatch(/小写/);
    }
  });

  it("前缀小写但模型名带大写 → 放行 (只有 provider 段敏感)", () => {
    // 如 azure/GPT-4o-Deployment: 部署名大小写是用户自己定的, 不该拦
    expect(saveError(
      { ...blank, name: "a", model: "azure/GPT-4o-Deployment" }, [])).toBeNull();
  });

  it("多级路径的模型名放行", () => {
    // 如 vertex_ai/publishers/google/models/gemini-2.5-pro、openrouter/x/y
    expect(saveError(
      { ...blank, name: "a", model: "openrouter/anthropic/claude-sonnet-5-5" },
      [])).toBeNull();
  });

  it("合法输入 → null", () => {
    expect(saveError(
      { name: "mid-glm", model: "anthropic/g-glm-5.3", api_key: "sk-1", base_url: "" },
      [mk({ name: "other" })])).toBeNull();
  });

  it("新增时不强制填 key", () => {
    // key 可以后补 (先存着配置, 拿到 key 再填), 真正的拦截在 CLI 启动和对话时
    expect(saveError(
      { name: "later", model: "anthropic/x", api_key: "", base_url: "" }, []))
      .toBeNull();
  });
});

describe("deleteDisabled / deleteTooltip", () => {
  it("active 条目不可删", () => {
    expect(deleteDisabled(mk({ active: true }))).toBe(true);
    expect(deleteTooltip(mk({ active: true }))).toMatch(/正在使用|先切换/);
  });

  it("非 active 可删", () => {
    expect(deleteDisabled(mk({ active: false }))).toBe(false);
  });

  it("只剩一条时也不可删 (那条必然是 active)", () => {
    expect(deleteDisabled(mk({ active: true }))).toBe(true);
  });
});

describe("sortModels", () => {
  it("active 置顶, 其余按名字", () => {
    const rows = [mk({ name: "c" }), mk({ name: "a" }), mk({ name: "b", active: true })];
    expect(sortModels(rows).map((r) => r.name)).toEqual(["b", "a", "c"]);
  });

  it("不改原数组", () => {
    const rows = [mk({ name: "c" }), mk({ name: "a" })];
    sortModels(rows);
    expect(rows.map((r) => r.name)).toEqual(["c", "a"]);
  });
});

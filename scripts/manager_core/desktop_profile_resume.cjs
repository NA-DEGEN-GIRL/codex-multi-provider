// Both the visible renderer and background managers open shared tasks. Route
// execution through the current profile's config, never the previous writer's
// provider registry or credentials. Ordinary same-provider model choices survive.
globalThis.__codexProfileResume = async function(manager, send, receiver, method, params) {
  if (!['thread/resume', 'thread/fork'].includes(method) || !params?.threadId) return params;
  const {config} = await Reflect.apply(send, receiver, ['config/read', {includeLayers:false}]);
  if (!config) throw Error('The current profile model configuration is unavailable.');
  const provider = config.model_provider || 'openai';
  // modelProvider in the request is a destination override, not evidence of
  // which provider wrote the saved task.
  let source = manager.threadStore?.threadsById?.get(params.threadId)?.modelProvider;
  if (!source) {
    const record = await Reflect.apply(send, receiver, ['thread/read', {threadId:params.threadId, includeTurns:false}]);
    source = record.thread?.modelProvider;
  }
  const result = {...params, modelProvider:provider, config:{...params.config, model_provider:provider}};
  if (source && source !== provider) {
    // Persisted per-thread model/effort belongs to the previous provider too.
    result.model = config.model || 'gpt-6-astra';
    result.config.model = result.model;
    result.config.model_reasoning_effort = config.model_reasoning_effort || 'medium';
  }
  return result;
};

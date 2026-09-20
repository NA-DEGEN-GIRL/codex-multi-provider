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
  // Another profile may have changed the durable provider/model since this
  // window cached its summary. Read metadata only, never the transcript.
  const record = await Reflect.apply(send, receiver, ['thread/read', {threadId:params.threadId, includeTurns:false}]);
  const source = record.thread?.modelProvider;
  const result = {...params, modelProvider:provider, config:{...params.config, model_provider:provider}};
  if (source === provider && params.model == null &&
      !Object.hasOwn(params.config || {}, 'model') &&
      !Object.hasOwn(params.config || {}, 'model_reasoning_effort')) {
    // Even a redundant provider override suppresses the runtime's automatic
    // model/effort restore. Carry the durable selection explicitly while still
    // enforcing this profile's provider. Explicit caller choices take precedence.
    if (typeof record.thread.model === 'string' && record.thread.model) {
      result.model = record.thread.model;
      if (typeof record.thread.reasoningEffort === 'string')
        result.config.model_reasoning_effort = record.thread.reasoningEffort;
    }
  }
  if (source && source !== provider) {
    // Persisted per-thread model/effort belongs to the previous provider too.
    result.model = config.model || 'gpt-6-astra';
    result.config.model = result.model;
    result.config.model_reasoning_effort = config.model_reasoning_effort || 'medium';
  }
  return result;
};

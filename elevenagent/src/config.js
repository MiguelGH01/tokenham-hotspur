export function loadConfig(env = process.env) {
  const agentId = env.ELEVEN_AGENT_ID || env.AGENT_ID;
  const required = ["ELEVENLABS_API_KEY", "CLINIC_API_KEY"];
  const missing = required.filter((key) => !env[key]);
  if (!agentId) missing.push("ELEVEN_AGENT_ID|AGENT_ID");
  if (missing.length) throw new Error(`Missing environment variables: ${missing.join(", ")}`);
  return {
    port: Number(env.PORT || 3000),
    elevenLabsApiKey: env.ELEVENLABS_API_KEY,
    agentId,
    clinicApiKey: env.CLINIC_API_KEY,
    clinicApiBaseUrl: (env.CLINIC_API_BASE_URL || "https://hackspain.getprosperapp.com/api").replace(/\/$/, ""),
    logLevel: env.LOG_LEVEL || "info",
    geocodingBaseUrl: env.GEOCODING_BASE_URL || "https://nominatim.openstreetmap.org/search",
    geocodingUserAgent: env.GEOCODING_USER_AGENT || "prosper-elevenlabs-bridge/1.1",
    obsIngestUrl: (env.OBS_INGEST_URL || "").replace(/\/$/, ""),
    obsIngestToken: env.OBS_INGEST_TOKEN || "",
  };
}

import axios from "axios";
import { msalInstance, apiScopes } from "../auth/msal";

const AUTH_DISABLED = import.meta.env.VITE_DISABLE_AUTH === "true";

export const client = axios.create({ baseURL: "/api" });

client.interceptors.request.use(async (config) => {
  if (AUTH_DISABLED) return config;
  const account = msalInstance.getAllAccounts()[0];
  if (!account) return config;
  try {
    const result = await msalInstance.acquireTokenSilent({ scopes: apiScopes, account });
    config.headers.Authorization = `Bearer ${result.accessToken}`;
  } catch {
    // ignore — request will 401 if token is required
  }
  return config;
});

/**
 * MSAL configuration. Populate from environment via Vite (.env.local):
 *   VITE_AZURE_TENANT_ID
 *   VITE_AZURE_CLIENT_ID
 *   VITE_API_SCOPE          e.g. api://<api-client-id>/access_as_user
 *
 * For local dev with `DOCMIND_DISABLE_AUTH=true` on the backend you can
 * simply skip MSAL — the API ignores the token.
 */
import { Configuration, PublicClientApplication } from "@azure/msal-browser";

export const msalConfig: Configuration = {
  auth: {
    clientId: import.meta.env.VITE_AZURE_CLIENT_ID || "00000000-0000-0000-0000-000000000000",
    authority: `https://login.microsoftonline.com/${import.meta.env.VITE_AZURE_TENANT_ID || "common"}`,
    redirectUri: window.location.origin,
  },
  cache: { cacheLocation: "sessionStorage", storeAuthStateInCookie: false },
};

export const apiScopes: string[] = [
  import.meta.env.VITE_API_SCOPE || "User.Read",
];

export const msalInstance = new PublicClientApplication(msalConfig);

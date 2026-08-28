const environment = process.env.NEXT_PUBLIC_APP_ENV ?? "dev";

const apiByEnvironment: Record<string, string | undefined> = {
  dev: process.env.NEXT_PUBLIC_API_URL_DEV,
  stage: process.env.NEXT_PUBLIC_API_URL_STAGE,
  prod: process.env.NEXT_PUBLIC_API_URL_PROD,
};

export const API = apiByEnvironment[environment] ?? "http://localhost:8000";

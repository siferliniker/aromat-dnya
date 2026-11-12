import { get, put } from '@vercel/blob';
import { handleApi } from '../lib/backend.mjs';
import { createBlobDatabase } from '../lib/blob-db.mjs';
import catalogue from '../lib/catalogue.json' with { type: 'json' };
import { secureResponse } from '../lib/security.mjs';

export default {
  async fetch(request) {
    let response;
    try {
      // Vercel forwards rewrite captures in the query. Consume our reserved
      // routing parameter before the backend validates product filters.
      const url = new URL(request.url);
      const routes = url.searchParams.getAll('__aromatia_route');
      if (routes.length > 1 || (routes.length && !/^[a-z0-9/_-]{1,80}$/.test(routes[0]))) {
        return secureResponse(Response.json({error:'invalid_request'},{status:400}),request);
      }
      if (routes.length) {
        url.pathname = '/api/' + routes[0];
        url.searchParams.delete('__aromatia_route');
        // The platform also appends the named source capture itself.
        const captures = url.searchParams.getAll('path');
        if (captures.length === 1 && captures[0] === routes[0]) url.searchParams.delete('path');
        request = new Request(url, request);
      }
      const env = { ...process.env, DB: createBlobDatabase({ get, put }) };
      response = await handleApi(request, env, catalogue) || Response.json({ error: 'not_found' }, { status: 404 });
    } catch (error) {
      console.error('API failure', error?.name || 'Error');
      response = Response.json({ error: 'temporarily_unavailable' }, { status: 503 });
    }
    return secureResponse(response, request);
  },
};

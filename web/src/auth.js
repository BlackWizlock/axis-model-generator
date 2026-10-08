import {api, setCsrfToken} from './api.js';
export async function guestSession(){const user=await api('/api/auth/guest',{method:'POST'});if(typeof user.csrfToken!=='string')throw new Error('Session unavailable');setCsrfToken(user.csrfToken);return user;}

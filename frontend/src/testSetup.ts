import {cleanup} from '@testing-library/react';
import {afterEach,beforeEach,vi} from 'vitest';
beforeEach(()=>{localStorage.clear();sessionStorage.clear();vi.spyOn(window,'scrollTo').mockImplementation(()=>{});});
afterEach(()=>{cleanup();vi.restoreAllMocks();});

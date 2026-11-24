import React from 'react';import {createRoot} from 'react-dom/client';import OrderFlowPanel from './OrderFlowPanel';import '../heatmap/heatmap.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><main><OrderFlowPanel/></main></React.StrictMode>);

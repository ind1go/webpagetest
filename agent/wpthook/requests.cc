/******************************************************************************
Copyright (c) 2010, Google Inc.
All rights reserved.

Redistribution and use in source and binary forms, with or without 
modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice, 
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the name of the <ORGANIZATION> nor the names of its contributors 
    may be used to endorse or promote products derived from this software 
    without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" 
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE 
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE 
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE 
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL 
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR 
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER 
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, 
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE 
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
******************************************************************************/

#include "StdAfx.h"
#include <Wininet.h>
#include "requests.h"
#include "test_state.h"
#include "track_dns.h"
#include "track_sockets.h"
#include "../wptdriver/wpt_test.h"


/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
Requests::Requests(TestState& test_state, TrackSockets& sockets,
                    TrackDns& dns, WptTest& test):
  _test_state(test_state)
  , _sockets(sockets)
  , _dns(dns)
  , _test(test) {
  _active_requests.InitHashTable(257);
  connections_.InitHashTable(257);
  InitializeCriticalSection(&cs);
  _start_browser_clock = 0;
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
Requests::~Requests(void) {
  Reset();

  DeleteCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::Reset() {
  EnterCriticalSection(&cs);
  _active_requests.RemoveAll();
  while (!_requests.IsEmpty())
    delete _requests.RemoveHead();
  browser_request_data_.RemoveAll();
  LeaveCriticalSection(&cs);
  _dns.ClaimAll();
  _sockets.ClaimAll();
  _start_browser_clock = 0;
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::Lock() {
  EnterCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::Unlock() {
  LeaveCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::SocketClosed(DWORD socket_id) {
  StreamClosed(socket_id, 0);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::DataIn(DWORD socket_id, DataChunk& chunk) {
  if (_test_state._active) {
    EnterCriticalSection(&cs);
    // See if socket maps to a known request.
    Request * request = NULL;
    ULARGE_INTEGER key;
    key.HighPart = socket_id;
    key.LowPart = 0;
    if (_active_requests.Lookup(key.QuadPart, request) && request) {
      _test_state.ActivityDetected();
      request->DataIn(chunk);
      WptTrace(loglevel::kFunction, 
               _T("[wpthook] - Requests::DataIn(socket_id=%d, len=%d)"),
               socket_id, chunk.GetLength());
    } else {
      WptTrace(loglevel::kFrequentEvent,
               _T("[wpthook] - Requests::DataIn(socket_id=%d, len=%d)")
               _T("   not associated with a known request"),
               socket_id, chunk.GetLength());
    }
    LeaveCriticalSection(&cs);
  }
}

/*-----------------------------------------------------------------------------
  Allow data to be modified.
-----------------------------------------------------------------------------*/
bool Requests::ModifyDataOut(DWORD socket_id, DataChunk& chunk) {
  bool is_modified = false;
  if (_test_state._active) {
    EnterCriticalSection(&cs);
    Request * request = GetOrCreateRequest(socket_id, 0, chunk);
    if (request) {
      _test_state.ActivityDetected();
      is_modified = request->ModifyDataOut(chunk);
    } else {
      is_modified = chunk.ModifyDataOut(_test);
    }
    WptTrace(loglevel::kFunction,
        _T("[wpthook] Requests::ModifyDataOut(socket_id=%d, len=%d) -> %d"),
        socket_id, chunk.GetLength(), is_modified);
    LeaveCriticalSection(&cs);
  }
  return is_modified;
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::DataOut(DWORD socket_id, DataChunk& chunk) {
  if (_test_state._active) {
    EnterCriticalSection(&cs);
    Request * request = GetOrCreateRequest(socket_id, 0, chunk);
    if (request) {
      _test_state.ActivityDetected();
      request->DataOut(chunk);
      WptTrace(loglevel::kFunction, 
               _T("[wpthook] - Requests::DataOut(socket_id=%d, len=%d)"),
               socket_id, chunk.GetLength());
    } else {
      WptTrace(loglevel::kFrequentEvent, 
               _T("[wpthook] - Requests::DataOut(socket_id=%d, len=%d)")
               _T("  Non-HTTP traffic detected"),
               socket_id, chunk.GetLength());
    }
    LeaveCriticalSection(&cs);
  }
}

/*-----------------------------------------------------------------------------
  A request is "active" once it is created by calling DataOut/DataIn.
-----------------------------------------------------------------------------*/
bool Requests::HasActiveRequest(DWORD socket_id, DWORD stream_id) {
  return GetActiveRequest(socket_id, stream_id) != NULL;
}

/*-----------------------------------------------------------------------------
  See if the beginning of the bugger matches any known HTTP method
  TODO: See if there is a more reliable way to detect HTTP traffic
-----------------------------------------------------------------------------*/
bool Requests::IsHttpRequest(const DataChunk& chunk) const {
  bool ret = false;
  const char * HTTP_METHODS[] = {"GET ", "HEAD ", "POST ", "PUT ",
      "OPTIONS ", "DELETE ", "TRACE ", "CONNECT ", "PATCH "};
  for (int i = 0; i < _countof(HTTP_METHODS) && !ret; i++) {
    const char * method = HTTP_METHODS[i];
    unsigned long method_len = strlen(method);
    if (chunk.GetLength() >= method_len &&
        !memcmp(chunk.GetData(), method, method_len)) {
      ret = true;
    }
  }
  return ret;
}

/*-----------------------------------------------------------------------------
  This must always be called from within a critical section.
-----------------------------------------------------------------------------*/
bool Requests::IsSpdyRequest(const DataChunk& chunk) const {
  bool is_spdy = false;
  const char *buf = chunk.GetData();
  if (chunk.GetLength() >= 8) {
    is_spdy = buf[0] == '\x80' && buf[1] == '\x02';  // SPDY control frame
  }
  return is_spdy;
}


 /*-----------------------------------------------------------------------------
   Find an existing request, or create a new one if appropriate.
 -----------------------------------------------------------------------------*/
Request * Requests::GetOrCreateRequest(DWORD socket_id, DWORD stream_id,
                                       const DataChunk& chunk) {
  Request * request = NULL;
  ULARGE_INTEGER key;
  key.HighPart = socket_id;
  key.LowPart = stream_id;
  if (_active_requests.Lookup(key.QuadPart, request) && request) {
    // We have an existing request on this socket, however, if data has been
    // received already, then this may be a new request.
    if (!request->_is_spdy && request->_response_data.GetDataSize() &&
        IsHttpRequest(chunk)) {
      request = NewRequest(socket_id, stream_id, false);
    }
  } else {
    bool is_spdy = IsSpdyRequest(chunk);
    if (is_spdy || IsHttpRequest(chunk)) {
      request = NewRequest(socket_id, stream_id, is_spdy);
    }
  }
  return request;
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
Request * Requests::NewRequest(DWORD socket_id, DWORD stream_id,
                               bool is_spdy) {
  Request * request = new Request(_test_state, socket_id, stream_id,
                                  _sockets, _dns, _test, is_spdy, *this);
  EnterCriticalSection(&cs);
  ULARGE_INTEGER key;
  key.HighPart = socket_id;
  key.LowPart = stream_id;
  _active_requests.SetAt(key.QuadPart, request);
  _requests.AddTail(request);
  LeaveCriticalSection(&cs);
  return request;
}

/*-----------------------------------------------------------------------------
  A request is "active" once it is created by calling DataOut/DataIn.
-----------------------------------------------------------------------------*/
Request * Requests::GetActiveRequest(DWORD socket_id, DWORD stream_id) {
  Request * request = NULL;
  ULARGE_INTEGER key;
  key.HighPart = socket_id;
  key.LowPart = stream_id;
  _active_requests.Lookup(key.QuadPart, request);
  return request;
}

/*-----------------------------------------------------------------------------
  Map a browser time to a perf counter time for a request
-----------------------------------------------------------------------------*/
LONGLONG Requests::GetRelativeTime(Request * request, double end_time, double time) {
  LONGLONG elapsed_ticks =
      (LONGLONG)((end_time - time) * _test_state._ms_frequency.QuadPart);
  return request->_end.QuadPart - elapsed_ticks;
}

/*-----------------------------------------------------------------------------
  Request information passed in from a browser-specific extension
  For now this is only Chrome and we only use it to get the initiator 
  information
-----------------------------------------------------------------------------*/
void Requests::ProcessBrowserRequest(CString request_data) {
  CString browser, url, initiator, initiator_line, initiator_column;
  CStringA request_headers, response_headers;
  double  start_time = 0, end_time = 0, first_byte = 0, request_start = 0,
          dns_start = -1, dns_end = -1, connect_start = -1, connect_end = -1,
          ssl_start = -1, ssl_end = -1;
  long connection = 0, error_code = 0, status = 0, bytes_in = 0, stream_id = 0;
  LARGE_INTEGER now;
  QueryPerformanceCounter(&now);
  bool processing_values = true;
  bool processing_request = false;
  bool processing_response = false;
  int position = 0;
  CString line = request_data.Tokenize(_T("\n"), position);
  while (position >= 0) {
    line = line.Trim();
    if (line.GetLength()) {
      if (!line.Left(1).Compare(_T("["))) {
        processing_values = false;
        processing_request = false;
        processing_response = false;
        if (!line.Compare(_T("[Request Headers]")))
          processing_request = true;
        else if (!line.Compare(_T("[Response Headers]")))
          processing_response = true;
      } else if (processing_values) {
        int separator = line.Find(_T('='));
        if (separator > 0) {
          CString key = line.Left(separator).Trim();
          CString value = line.Mid(separator + 1).Trim();
          if (key.GetLength() && value.GetLength()) {
            if (!key.CompareNoCase(_T("browser")))
              browser = value;
            else if (!key.CompareNoCase(_T("url")))
              url = value;
            else if (!key.CompareNoCase(_T("errorCode")))
              error_code = _ttol(value);
            else if (!key.CompareNoCase(_T("startTime")))
              start_time = _ttof(value);
            else if (!key.CompareNoCase(_T("requestStart")))
              request_start = _ttof(value);
            else if (!key.CompareNoCase(_T("firstByteTime")))
              first_byte = _ttof(value);
            else if (!key.CompareNoCase(_T("endTime")))
              end_time = _ttof(value);
            else if (!key.CompareNoCase(_T("bytesIn")))
              bytes_in = _ttol(value);
            else if (!key.CompareNoCase(_T("initiatorUrl")))
              initiator = value;
            else if (!key.CompareNoCase(_T("initiatorLineNumber")))
              initiator_line = value;
            else if (!key.CompareNoCase(_T("initiatorColumnNumber")))
              initiator_column = value;
            else if (!key.CompareNoCase(_T("status")))
              status = _ttol(value);
            else if (!key.CompareNoCase(_T("connectionId")))
              connection = _ttol(value);
            else if (!key.CompareNoCase(_T("dnsStart")))
              dns_start = _ttof(value);
            else if (!key.CompareNoCase(_T("dnsEnd")))
              dns_end = _ttof(value);
            else if (!key.CompareNoCase(_T("connectStart")))
              connect_start = _ttof(value);
            else if (!key.CompareNoCase(_T("connectEnd")))
              connect_end = _ttof(value);
            else if (!key.CompareNoCase(_T("sslStart")))
              ssl_start = _ttof(value);
            else if (!key.CompareNoCase(_T("sslEnd")))
              ssl_end = _ttof(value);
          }
        }
      } else if (processing_request) {
        request_headers += CStringA(CT2A(line)) + "\r\n";
      } else if (processing_response) {
        response_headers += CStringA(CT2A(line)) + "\r\n";
      }
    }
    line = request_data.Tokenize(_T("\n"), position);
  }
  if (url.GetLength() && initiator.GetLength()) {
    BrowserRequestData data(url);
    data.initiator_ = initiator;
    data.initiator_line_ = initiator_line;
    data.initiator_column_ = initiator_column;
    EnterCriticalSection(&cs);
    browser_request_data_.AddTail(data);
    LeaveCriticalSection(&cs);
  }
  _test_state.ActivityDetected();
  if (end_time > 0 && request_start > 0) {
    Request * request = new Request(_test_state, connection, stream_id,
                                    _sockets, _dns, _test, false, *this);
    request->_from_browser = true;
    request->initiator_ = initiator;
    request->initiator_line_ = initiator_line;
    request->initiator_column_ = initiator_column;
    request->_bytes_in = bytes_in;

    // See if we can map the browser's internal clock timestamps to our
    // performance counters.  If we have a DNS lookup we can match up or a
    // likely socket connect then we should be able to.
    if (_start_browser_clock == 0) {
      if (dns_end != -1) {
        // get the host name
        URL_COMPONENTS parts;
        memset(&parts, 0, sizeof(parts));
        TCHAR szHost[10000];
        memset(szHost, 0, sizeof(szHost));
        parts.lpszHostName = szHost;
        parts.dwHostNameLength = _countof(szHost);
        parts.dwStructSize = sizeof(parts);
        if (InternetCrackUrl((LPCTSTR)url, 0, 0, &parts)) {
          CString host(szHost);
          DNSAddressList addresses;
          LARGE_INTEGER match_dns_start, match_dns_end;
          if (_dns.Find(host, addresses, match_dns_start, match_dns_end)) {
            // Figure out what the clock time would have been at our perf
            // counter start time.
            _start_browser_clock =
                dns_end - _test_state.ElapsedMsFromStart(match_dns_end);
          }
        }
      }
    }

    bool already_connected = false;
    if (connection) {
      connections_.Lookup(connection, already_connected);
      connections_.SetAt(connection, true);
    }
    if (!url.Left(6).Compare(_T("https:")))
      request->_is_ssl = true;
    else
      request->_is_ssl = false;

    // figure out the conversion from browser time to perf counter
    LONGLONG ms_freq = _test_state._ms_frequency.QuadPart;
    if (_start_browser_clock != 0) {
      request->_end.QuadPart = _test_state._start.QuadPart +
          (LONGLONG)((end_time - _start_browser_clock)  * ms_freq);
    } else {
      request->_end.QuadPart = now.QuadPart;
      _start_browser_clock = end_time - _test_state.ElapsedMsFromStart(request->_end);
    }
    request->_start.QuadPart = request->_end.QuadPart - 
                (LONGLONG)((end_time - request_start) * ms_freq);
    if (first_byte > 0) {
      request->_first_byte.QuadPart = request->_end.QuadPart - 
                (LONGLONG)((end_time - first_byte) * ms_freq);
    }
    // if we have request timing info, the real start is sent directly
    if (!already_connected) {
      if (dns_start > -1 && dns_end > -1) {
        request->_dns_start.QuadPart = GetRelativeTime(request, end_time, dns_start);
        request->_dns_end.QuadPart = GetRelativeTime(request, end_time, dns_end);
      }
      if (connect_start > -1 && connect_end > -1) {
        if (ssl_start > -1 && ssl_end > -1) {
          request->_ssl_start.QuadPart = GetRelativeTime(request, end_time, ssl_start);
          request->_ssl_end.QuadPart = GetRelativeTime(request, end_time, ssl_end);
        }
        request->_connect_start.QuadPart = GetRelativeTime(request, end_time, connect_start);
        request->_connect_end.QuadPart = GetRelativeTime(request, end_time, connect_end);
      }
    }
    if (request_headers.GetLength()) {
      request_headers += "\r\n";
      DataChunk chunk((LPCSTR)request_headers, request_headers.GetLength());
      request->_request_data.AddChunk(chunk);
    }
    if (response_headers.GetLength()) {
      response_headers += "\r\n";
      DataChunk chunk((LPCSTR)response_headers, response_headers.GetLength());
      request->_response_data.AddChunk(chunk);
    }

    // Do a sanity check and throw out any requests that have bogus timings.
    // Chrome bug: https://code.google.com/p/chromium/issues/detail?id=309570
    LONGLONG slop = _test_state._ms_frequency.QuadPart * 10000;
    LARGE_INTEGER earliest, latest;
    earliest.QuadPart = _test_state._start.QuadPart - slop;
    latest.QuadPart = now.QuadPart + slop;
    if (request->_start.QuadPart > earliest.QuadPart &&
        request->_end.QuadPart < latest.QuadPart &&
        (!request->_first_byte.QuadPart ||
         (request->_first_byte.QuadPart > earliest.QuadPart &&
          request->_first_byte.QuadPart < latest.QuadPart)) &&
        (!request->_connect_start.QuadPart ||
         (request->_connect_start.QuadPart > earliest.QuadPart &&
          request->_connect_start.QuadPart < latest.QuadPart)) &&
        (!request->_connect_end.QuadPart ||
         (request->_connect_end.QuadPart > earliest.QuadPart &&
          request->_connect_end.QuadPart < latest.QuadPart)) &&
        (!request->_dns_start.QuadPart ||
         (request->_dns_start.QuadPart > earliest.QuadPart &&
          request->_dns_start.QuadPart < latest.QuadPart)) &&
        (!request->_dns_end.QuadPart ||
         (request->_dns_end.QuadPart > earliest.QuadPart &&
          request->_dns_end.QuadPart < latest.QuadPart)) &&
        (!request->_ssl_start.QuadPart ||
         (request->_ssl_start.QuadPart > earliest.QuadPart &&
          request->_ssl_start.QuadPart < latest.QuadPart)) &&
        (!request->_ssl_end.QuadPart ||
         (request->_ssl_end.QuadPart > earliest.QuadPart &&
          request->_ssl_end.QuadPart < latest.QuadPart))) {
      EnterCriticalSection(&cs);
      _requests.AddTail(request);
      LeaveCriticalSection(&cs);
    }
  }
}

/*-----------------------------------------------------------------------------
  Get the browser request information from the URL and optionally remove it
  from the list (claiming it)
-----------------------------------------------------------------------------*/
bool Requests::GetBrowserRequest(BrowserRequestData &data, bool remove) {
  bool found = false;

  EnterCriticalSection(&cs);
  POSITION pos = browser_request_data_.GetHeadPosition();
  while (pos && !found) {
    POSITION current_pos = pos;
    BrowserRequestData browser_data = browser_request_data_.GetNext(pos);
    if (!browser_data.url_.Compare(data.url_)) {
      found = true;
      data = browser_data;
      if (remove) {
        browser_request_data_.RemoveAt(current_pos);
      }
    }
  }
  browser_request_data_.AddTail(data);
  LeaveCriticalSection(&cs);

  return found;
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::StreamClosed(DWORD socket_id, DWORD stream_id) {
  EnterCriticalSection(&cs);
  Request * request = NULL;
  ULARGE_INTEGER key;
  key.HighPart = socket_id;
  key.LowPart = stream_id;
  if (_active_requests.Lookup(key.QuadPart, request) && request) {
    request->SocketClosed();
    _active_requests.RemoveKey(key.QuadPart);
  }
  LeaveCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::HeaderIn(DWORD socket_id, DWORD stream_id,
                        const char * header, const char * value, bool pushed) {
  EnterCriticalSection(&cs);
  Request * request = GetActiveRequest(socket_id, stream_id);
  if (!request)
    request = NewRequest(socket_id, stream_id, false);
  if (request)
    request->HeaderIn(header, value, pushed);
  LeaveCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::ObjectDataIn(DWORD socket_id, DWORD stream_id,
                            DataChunk& chunk) {
  EnterCriticalSection(&cs);
  Request * request = GetActiveRequest(socket_id, stream_id);
  if (request)
    request->ObjectDataIn(chunk);
  LeaveCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::BytesIn(DWORD socket_id, DWORD stream_id, size_t len) {
  EnterCriticalSection(&cs);
  Request * request = GetActiveRequest(socket_id, stream_id);
  if (request)
    request->BytesIn(len);
  LeaveCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::BytesOut(DWORD socket_id, DWORD stream_id, size_t len) {
  EnterCriticalSection(&cs);
  Request * request = GetActiveRequest(socket_id, stream_id);
  if (request)
    request->BytesOut(len);
  LeaveCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::HeaderOut(DWORD socket_id, DWORD stream_id, const char * header,
                         const char * value, bool pushed) {
  EnterCriticalSection(&cs);
  Request * request = GetActiveRequest(socket_id, stream_id);
  if (!request)
    request = NewRequest(socket_id, stream_id, false);
  if (request)
    request->HeaderOut(header, value, pushed);
  LeaveCriticalSection(&cs);
}

/*-----------------------------------------------------------------------------
-----------------------------------------------------------------------------*/
void Requests::ObjectDataOut(DWORD socket_id, DWORD stream_id,
                             DataChunk& chunk) {
  EnterCriticalSection(&cs);
  Request * request = GetActiveRequest(socket_id, stream_id);
  if (!request)
    request = NewRequest(socket_id, stream_id, false);
  if (request)
    request->ObjectDataOut(chunk);
  LeaveCriticalSection(&cs);
}

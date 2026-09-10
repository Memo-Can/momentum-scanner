import { useCallback, useEffect, useRef, useState } from 'react';
import { BackHandler, Platform, SafeAreaView, StatusBar, StyleSheet } from 'react-native';
import WebView from 'react-native-webview';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';

// Web dashboard'un canli adresi - mobil uygulama bunu bir WebView icinde
// aciyor. Web'de yapilan HER degisiklik (yeni sutun, ozellik, tasarim vb.)
// ayri bir mobil surum yayinlamaya gerek kalmadan otomatik olarak mobilde
// de gorunur - WebView yaklasiminin tum amaci bu.
const WEB_URL = 'https://momentumscanner.com.tr';

// Uygulama on planda acikken gelen bildirimlerin nasil gosterilecegini
// belirler (SDK 57: shouldShowBanner/shouldShowList, eski shouldShowAlert
// artik kullanilmiyor).
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

// Bildirime dokunuldugunda (uygulama kapaliyken/arka plandayken) WebView
// henuz yuklenmemis olabilir - bu durumda hangi hisseye gidilecegi burada
// tutulur, WebView yuklendiginde (onLoadEnd) islenir.
async function registerForPushNotificationsAsync(): Promise<void> {
  if (!Device.isDevice) {
    // Emulator/simulator Google Play Services icermiyorsa token alinamaz -
    // bizim kullandigimiz "Google Play imajli" emulator bir gercek cihaz
    // gibi davranir, Device.isDevice burada true doner.
    console.log('Fiziksel cihaz/emulator degil, push bildirimi atlanir.');
    return;
  }

  if (Platform.OS === 'android') {
    // Android 13+ icin bildirim izni istemeden ONCE kanal olusturulmali.
    await Notifications.setNotificationChannelAsync('default', {
      name: 'Guclu AL/SAT Sinyalleri',
      importance: Notifications.AndroidImportance.MAX,
      vibrationPattern: [0, 250, 250, 250],
      lightColor: '#e8c547',
    });
  }

  const { status } = await Notifications.requestPermissionsAsync();
  if (status !== 'granted') {
    console.log('Bildirim izni verilmedi.');
    return;
  }

  // ONEMLI: getExpoPushTokenAsync() DEGIL, getDevicePushTokenAsync() -
  // backend (scanner_bistTop100.py) bizim kendi Firebase projemize
  // dogrudan firebase-admin ile FCM token'i gonderiyor, Expo'nun kendi
  // push servisini kullanmiyor. Bu yuzden ham platform token'i (FCM)
  // gerekiyor, Expo'nun sardigi "ExponentPushToken[...]" degil.
  const devicePushToken = await Notifications.getDevicePushTokenAsync();

  try {
    await fetch(`${WEB_URL}/api/register-device`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: devicePushToken.data, platform: Platform.OS }),
    });
  } catch (e) {
    console.log('Push token backend\'e kaydedilemedi:', e);
  }
}

export default function App() {
  const webViewRef = useRef<WebView>(null);
  const [webViewLoaded, setWebViewLoaded] = useState(false);
  const pendingTickerRef = useRef<string | null>(null);

  // Bir hisseye ("ticker") git - WebView zaten yuklenmisse sayfayi baştan
  // yuklemeden dogrudan mevcut selectTicker() JS fonksiyonunu cagirir
  // (templates/index.html'de zaten tanimli); WebView henuz yuklenmediyse
  // istegi kuyruga alir, onLoadEnd'de isler.
  const openTicker = useCallback((ticker: string) => {
    const upperTicker = ticker.toUpperCase();
    if (webViewLoaded && webViewRef.current) {
      webViewRef.current.injectJavaScript(
        `window.selectTicker && window.selectTicker(${JSON.stringify(upperTicker)}); true;`
      );
    } else {
      pendingTickerRef.current = upperTicker;
    }
  }, [webViewLoaded]);

  useEffect(() => {
    registerForPushNotificationsAsync();

    // Uygulama ACIKKEN bir bildirime dokunulursa (foreground/background,
    // kapali degil) burada yakalanir.
    const responseSub = Notifications.addNotificationResponseReceivedListener((response) => {
      const ticker = response.notification.request.content.data?.ticker;
      if (typeof ticker === 'string') {
        openTicker(ticker);
      }
    });

    // Uygulama TAMAMEN KAPALIYKEN bir bildirime dokunulup uygulama bunun
    // uzerine acildiysa, son bildirim burada okunur (soguk baslangic).
    Notifications.getLastNotificationResponseAsync().then((response) => {
      const ticker = response?.notification.request.content.data?.ticker;
      if (typeof ticker === 'string') {
        openTicker(ticker);
      }
    });

    return () => responseSub.remove();
  }, [openTicker]);

  // Android geri tusu: WebView icinde geri gidilecek sayfa varsa once onu
  // kullan, yoksa varsayilan davranisa (uygulamadan cik) birak.
  useEffect(() => {
    if (Platform.OS !== 'android') return;
    const sub = BackHandler.addEventListener('hardwareBackPress', () => {
      if (webViewRef.current) {
        webViewRef.current.injectJavaScript(
          'window.history.length > 1 && window.history.back(); true;'
        );
        return true;
      }
      return false;
    });
    return () => sub.remove();
  }, []);

  return (
    <SafeAreaView style={styles.container}>
      <StatusBar barStyle="light-content" backgroundColor="#0f1115" />
      <WebView
        ref={webViewRef}
        source={{ uri: WEB_URL }}
        style={styles.webview}
        onLoadEnd={() => {
          setWebViewLoaded(true);
          if (pendingTickerRef.current) {
            openTicker(pendingTickerRef.current);
            pendingTickerRef.current = null;
          }
        }}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#0f1115' },
  webview: { flex: 1, backgroundColor: '#0f1115' },
});

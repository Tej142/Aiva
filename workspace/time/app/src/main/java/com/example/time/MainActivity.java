package com.example.time;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.widget.*;
import android.view.View;

public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);

        TextView tv = findViewById(R.id.tv);
        TimePicker tp = findViewById(R.id.tp);

        tp.setOnTimeChangedListener(new TimePicker.OnTimeChangedListener(){
            public void onTimeChanged(TimePicker view,int hourOfDay, int minute){
                tv.setText("Updated time: " + hourOfDay + ":" + minute);
            }
        });
    }
}